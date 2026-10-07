"""Offline behavioral tests; no Microsoft tenant, credentials or live writes."""

import importlib.util
import json
import sys
import types
from pathlib import Path

import httpx
import pytest

# Load this file independently of the adapter/Teams SDK and mutable package name.
MODULE_PATH = Path(__file__).parents[1] / "graph_files.py"
spec = importlib.util.spec_from_file_location("hermes_teams_graph_files_tested", MODULE_PATH)
gf = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gf
spec.loader.exec_module(gf)

TEAM, CHANNEL = "team-id", "19:channel@thread.tacv2"
SITE = "https://contoso.sharepoint.com/sites/Example"
LIBRARY = SITE + "/Shared%20Documents"
FILE_URL = LIBRARY + "/General/report.docx"
DOWNLOAD = "https://contoso.sharepoint.com/download?secret=never-log-this"
FOLDER = {"id": "folder", "webUrl": LIBRARY + "/General", "parentReference": {"driveId": "drive"}}
ITEM = {"id": "item", "name": "report.docx", "webUrl": FILE_URL, "size": 4,
        "file": {"mimeType": "application/test"}, "@microsoft.graph.downloadUrl": DOWNLOAD}


class TokenProvider:
    def __init__(self, token="profile-a-token"):
        self.token, self.calls = token, []

    async def get_access_token(self, *, force_refresh=False):
        self.calls.append(force_refresh)
        return self.token


class Harness:
    def __init__(self, graph_handler, transfer_handler=None, **kwargs):
        self.requests, self.transfers, self.sleeps = [], [], []
        self.provider = TokenProvider()

        def graph(request):
            self.requests.append(request)
            assert request.url.host == "graph.microsoft.com"
            assert request.headers["Authorization"] == "Bearer profile-a-token"
            return graph_handler(request)

        def transfer(request):
            self.transfers.append(request)
            assert "Authorization" not in request.headers
            assert "Cookie" not in request.headers
            assert transfer_handler is not None, "Unexpected transfer request"
            return transfer_handler(request)

        async def sleep(seconds):
            self.sleeps.append(seconds)

        self.graph = httpx.AsyncClient(transport=httpx.MockTransport(graph))
        # The service must strip even accidentally supplied default credentials.
        self.transfer = httpx.AsyncClient(transport=httpx.MockTransport(transfer),
                                         headers={"Authorization": "accidental-token", "Cookie": "session=secret"})
        self.service = gf.GraphFileClient(self.provider, graph_client=self.graph, transfer_client=self.transfer,
                                          sleep=sleep, **kwargs)


def folder_then_root_then_item(request):
    if request.url.path.endswith("/filesFolder"):
        return httpx.Response(200, json=FOLDER)
    if request.url.path.endswith("/root"):
        return httpx.Response(200, json={"id": "root", "webUrl": LIBRARY})
    return httpx.Response(200, json=ITEM)


@pytest.mark.asyncio
@pytest.mark.parametrize("root,expected", [(None, "/messages/reply"), ("root", "/messages/root/replies/reply"), ("reply", "/messages/reply")])
async def test_exact_message_and_reply_routing(root, expected):
    attachments = [{"contentType": "reference", "contentUrl": FILE_URL, "name": "file.docx"},
                   {"contentType": "application/vnd.microsoft.card.adaptive"}, {"contentType": "text/html"}]
    h = Harness(lambda r: httpx.Response(200, json={"attachments": attachments}))
    result = await h.service.get_message_attachments(TEAM, CHANNEL, "reply", root_id=root)
    assert result == attachments[:1]
    assert h.requests[0].url.path.endswith(expected)
    assert b"19%3Achannel%40thread.tacv2" in h.requests[0].url.raw_path


@pytest.mark.asyncio
async def test_explicit_thread_retrieval_paginates_and_keeps_source_ids():
    attachment = {"contentType": "reference", "contentUrl": FILE_URL, "id": "a"}

    def handler(request):
        if request.url.path.endswith("/messages/root"):
            return httpx.Response(200, json={"id": "root", "attachments": [attachment]})
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json={"value": [{"id": "reply2", "attachments": [dict(attachment, id="b", contentUrl=FILE_URL + "2")]}]})
        return httpx.Response(200, json={"value": [{"id": "reply1", "attachments": [attachment]}],
                                        "@odata.nextLink": str(request.url.copy_set_param("page", "2"))})

    h = Harness(handler)
    files = await h.service.get_thread_attachments(TEAM, CHANNEL, "root")
    assert len(files) == 2  # Duplicate attachment on a reply is not downloaded twice.
    assert files[1]["_message_id"] == "reply2" and files[1]["_root_id"] == "root"
    assert len(h.requests) == 3


@pytest.mark.asyncio
async def test_thread_attachment_cap_stops_without_fetching_more_pages():
    def handler(request):
        if request.url.path.endswith("/messages/root"):
            return httpx.Response(200, json={"id": "root", "attachments": []})
        return httpx.Response(200, json={"value": [{"id": str(i), "attachments": [{"contentType": "reference", "id": str(i), "contentUrl": FILE_URL + str(i)}]} for i in range(50)],
                                        "@odata.nextLink": str(request.url)})

    h = Harness(handler)
    files = await h.service.get_thread_attachments(TEAM, CHANNEL, "root")
    assert len(files) == 20 and len(h.requests) == 2


@pytest.mark.asyncio
async def test_thread_message_cap_and_malicious_pagination_url():
    def handler(request):
        if request.url.path.endswith("/messages/root"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"value": [{"id": str(i)} for i in range(50)], "@odata.nextLink": "https://evil.test/steal"})

    h = Harness(handler)
    assert await h.service.get_thread_attachments(TEAM, CHANNEL, "root", max_messages=2) == []
    assert len(h.requests) == 2
    with pytest.raises(gf.GraphFileError, match="outside Microsoft Graph"):
        await h.service.get_thread_attachments(TEAM, CHANNEL, "root")


@pytest.mark.asyncio
async def test_reference_resolves_in_channel_drive_without_shares_or_site_enumeration():
    h = Harness(folder_then_root_then_item)
    item = await h.service.resolve_reference(FILE_URL, TEAM, CHANNEL)
    assert item["parentReference"]["driveId"] == "drive"
    assert len(h.requests) == 3
    assert h.requests[-1].url.path == "/v1.0/drives/drive/root:/General/report.docx"
    assert not any("/shares/" in r.url.path or "/sites/" in r.url.path for r in h.requests)


@pytest.mark.asyncio
async def test_office_path_url_unwraps_r_prefix_and_encodes_real_filename():
    h = Harness(folder_then_root_then_item)
    url = "https://contoso.sharepoint.com/:w:/r/sites/Example/Shared%20Documents/General/A%23B.docx?d=123"
    await h.service.resolve_reference(url, TEAM, CHANNEL)
    assert h.requests[-1].url.path.endswith("/General/A#B.docx")
    assert b"A%23B.docx" in h.requests[-1].url.raw_path


@pytest.mark.asyncio
async def test_other_library_uses_only_explicit_authorized_site_drives():
    url = SITE + "/Legal%20Documents/brief.pdf"

    def handler(request):
        p = request.url.path
        if p.endswith("/filesFolder"):
            return httpx.Response(200, json=FOLDER)
        if p == "/v1.0/drives/drive/root":
            return httpx.Response(200, json={"webUrl": LIBRARY})
        if p == "/v1.0/sites/contoso.sharepoint.com:/sites/Example":
            return httpx.Response(200, json={"id": "site,id"})
        if p == "/v1.0/sites/site,id/drives":
            return httpx.Response(200, json={"value": [{"id": "legal", "webUrl": SITE + "/Legal%20Documents"}]})
        assert p == "/v1.0/drives/legal/root:/brief.pdf"
        return httpx.Response(200, json=ITEM)

    h = Harness(handler)
    result = await h.service.resolve_reference(url, TEAM, CHANNEL)
    assert result["parentReference"]["driveId"] == "legal"


@pytest.mark.asyncio
async def test_cross_tenant_reference_rejected_before_drive_or_site_lookup():
    h = Harness(folder_then_root_then_item)
    with pytest.raises(gf.GraphFileError, match="different SharePoint host"):
        await h.service.resolve_reference(FILE_URL.replace("contoso", "other"), TEAM, CHANNEL)
    assert len(h.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://contoso.sharepoint.com/:w:/s/Example/opaque", SITE + "/_layouts/15/Doc.aspx?sourcedoc=guid"])
async def test_opaque_sharing_links_fail_clearly_without_broad_shares_endpoint(url):
    h = Harness(folder_then_root_then_item)
    with pytest.raises(gf.GraphFileError, match="direct URL or drive/item"):
        await h.service.resolve_reference(url, TEAM, CHANNEL)
    assert h.requests == []


@pytest.mark.asyncio
async def test_explicit_drive_item_download_returns_bytes_filename_and_mime():
    h = Harness(lambda r: httpx.Response(200, json=ITEM), lambda r: httpx.Response(200, content=b"data"))
    result = await h.service.download_attachment({"driveId": "drive", "itemId": "item"}, team_id=TEAM, channel_id=CHANNEL)
    assert (result.filename, result.data, result.mime) == ("report.docx", b"data", "application/test")
    assert h.requests[0].url.path.endswith("/drives/drive/items/item")
    assert len(h.transfers) == 1


@pytest.mark.asyncio
async def test_reference_download_resolves_then_fetches_without_graph_token():
    h = Harness(folder_then_root_then_item, lambda r: httpx.Response(200, content=b"data"))
    result = await h.service.download_attachment({"contentType": "reference", "contentUrl": FILE_URL}, team_id=TEAM, channel_id=CHANNEL)
    assert result.data == b"data"
    assert len(h.requests) == 3 and len(h.transfers) == 1


@pytest.mark.asyncio
async def test_download_redirects_are_bounded_and_unauthenticated():
    def transfer(request):
        if request.url.path == "/download":
            return httpx.Response(302, headers={"Location": "https://public.dm.files.1drv.com/final?secret=second"})
        return httpx.Response(200, content=b"done", headers={"Content-Type": "text/plain"})

    h = Harness(folder_then_root_then_item, transfer)
    data, mime = await h.service._download(DOWNLOAD)
    assert (data, mime) == (b"done", "text/plain")
    assert len(h.transfers) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://contoso.sharepoint.com/f", "https://127.0.0.1/f", "https://contoso.sharepoint.com.evil.test/f",
                                 "https://user:pass@contoso.sharepoint.com/f", "https://contoso.sharepoint.com:444/f",
                                 "https://localhost/f", "https://evil.test/f"])
async def test_unsafe_transfer_urls_never_reach_transport(url):
    h = Harness(folder_then_root_then_item)
    with pytest.raises(gf.GraphFileError, match="unsafe or unsupported"):
        await h.service._download(url)
    assert h.transfers == []


@pytest.mark.asyncio
async def test_redirect_to_private_host_is_blocked_without_leaking_session_url():
    h = Harness(folder_then_root_then_item, lambda r: httpx.Response(302, headers={"Location": "https://127.0.0.1/?secret=private"}))
    with pytest.raises(gf.GraphFileError) as error:
        await h.service._download(DOWNLOAD)
    assert "secret" not in str(error.value)
    assert len(h.transfers) == 1


@pytest.mark.asyncio
async def test_dns_safety_exception_is_redacted():
    def handler(request):
        raise ValueError("Private address found for " + DOWNLOAD)

    h = Harness(folder_then_root_then_item, handler)
    with pytest.raises(gf.GraphFileError, match="safety check") as error:
        await h.service._download(DOWNLOAD)
    assert "secret" not in str(error.value)


class ChunkedBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"1234"
        yield b"5678"


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}, {"Content-Length": "99999"}])
async def test_download_size_limit_handles_missing_lying_or_large_content_length(headers):
    h = Harness(folder_then_root_then_item, lambda r: httpx.Response(200, headers=headers, stream=ChunkedBody()), max_file_bytes=5)
    with pytest.raises(gf.GraphFileError, match="size limit"):
        await h.service._download(DOWNLOAD)


@pytest.mark.asyncio
async def test_known_oversized_metadata_prevents_download():
    h = Harness(lambda r: httpx.Response(200, json={**ITEM, "size": 100}), max_file_bytes=5)
    with pytest.raises(gf.GraphFileError, match="size limit"):
        await h.service.download_attachment({"driveId": "d", "itemId": "i"}, team_id=TEAM, channel_id=CHANNEL)
    assert h.transfers == []


@pytest.mark.asyncio
async def test_graph_never_follows_redirect_or_sends_bearer_to_nextlink_host():
    h = Harness(lambda r: httpx.Response(302, headers={"Location": DOWNLOAD}))
    with pytest.raises(gf.GraphFileError):
        await h.service.get_drive_item("d", "i")
    assert len(h.requests) == 1 and not h.transfers
    with pytest.raises(gf.GraphFileError, match="outside Microsoft Graph"):
        await h.service._request("GET", DOWNLOAD)
    assert len(h.provider.calls) == 1


@pytest.mark.asyncio
async def test_401_refreshes_only_this_clients_provider():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(401 if count == 1 else 200, json={} if count == 1 else ITEM)

    h = Harness(handler)
    await h.service.get_drive_item("d", "i")
    assert h.provider.calls == [False, True]


@pytest.mark.asyncio
async def test_429_retry_after_is_bounded_and_eventually_errors_without_remote_secrets():
    h = Harness(lambda r: httpx.Response(429, headers={"Retry-After": "999999"},
                                         json={"error": {"message": "Bearer secret " + DOWNLOAD}}))
    with pytest.raises(gf.GraphFileError) as error:
        await h.service.get_drive_item("d", "i")
    assert error.value.status_code == 429
    assert "secret" not in str(error.value) and "sharepoint" not in str(error.value)
    assert h.sleeps == [30, 30]
    assert len(h.requests) == 3


@pytest.mark.asyncio
async def test_permission_error_is_clear_redacted_and_not_retried():
    h = Harness(lambda r: httpx.Response(403, json={"error": {"message": DOWNLOAD}}))
    with pytest.raises(gf.GraphFileError, match="profile's Graph consent") as error:
        await h.service.get_channel_folder(TEAM, CHANNEL)
    assert "never-log" not in str(error.value)
    assert len(h.requests) == 1


@pytest.mark.asyncio
async def test_small_upload_uses_real_channel_drive_renames_and_never_creates_sharing_link(tmp_path):
    path = tmp_path / "original.txt"
    path.write_bytes(b"hello")

    def handler(request):
        if request.url.path.endswith("/filesFolder"):
            return httpx.Response(200, json=FOLDER)
        assert request.method == "PUT"
        assert request.url.path == "/v1.0/drives/drive/items/folder:/safe #name.txt:/content"
        assert request.url.params["@microsoft.graph.conflictBehavior"] == "rename"
        assert request.content == b"hello"
        assert request.headers["Content-Type"] == "text/plain"
        return httpx.Response(201, json={"id": "new", "webUrl": FILE_URL, "name": "safe #name (1).txt"})

    h = Harness(handler)
    result = await h.service.upload_channel_file(TEAM, CHANNEL, path, filename="safe #name.txt")
    assert result["id"] == "new" and result["parentReference"]["driveId"] == "drive"
    assert len(h.requests) == 2 and not h.transfers
    assert not any("createLink" in r.url.path or "/chats/" in r.url.path for r in h.requests)


@pytest.mark.asyncio
async def test_upload_session_sends_sequential_aligned_ranges_no_bearer(tmp_path):
    path = tmp_path / "big.bin"
    data = b"x" * (gf.SIMPLE_UPLOAD_MAX_BYTES + 123)
    path.write_bytes(data)
    chunks = []

    def graph(request):
        if request.url.path.endswith("/filesFolder"):
            return httpx.Response(200, json=FOLDER)
        assert request.url.path.endswith(":/createUploadSession")
        assert json.loads(request.content)["item"]["@microsoft.graph.conflictBehavior"] == "rename"
        return httpx.Response(200, json={"uploadUrl": "https://contoso.sharepoint.com/upload?secret=upload-token"})

    def transfer(request):
        assert request.method == "PUT"
        start = sum(len(c) for c in chunks)
        chunks.append(request.content)
        end = start + len(request.content)
        assert request.headers["Content-Range"] == f"bytes {start}-{end - 1}/{len(data)}"
        if end < len(data):
            assert len(request.content) % (320 * 1024) == 0
            return httpx.Response(202, json={"nextExpectedRanges": [f"{end}-"]})
        return httpx.Response(201, json={"id": "big", "webUrl": FILE_URL})

    h = Harness(graph, transfer)
    result = await h.service.upload_channel_file(TEAM, CHANNEL, path)
    assert result["id"] == "big" and b"".join(chunks) == data
    assert len(chunks) == 2


@pytest.mark.asyncio
async def test_unsafe_upload_session_is_rejected_before_bytes_leave_graph(tmp_path):
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * (gf.SIMPLE_UPLOAD_MAX_BYTES + 1))
    h = Harness(lambda r: httpx.Response(200, json=FOLDER if r.url.path.endswith("/filesFolder") else {"uploadUrl": "https://evil.test/upload"}))
    with pytest.raises(gf.GraphFileError, match="unsafe or unsupported"):
        await h.service.upload_channel_file(TEAM, CHANNEL, path)
    assert not h.transfers


@pytest.mark.asyncio
async def test_large_upload_requires_completed_item_and_consistent_ranges(tmp_path):
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * (gf.SIMPLE_UPLOAD_MAX_BYTES + 1))
    h = Harness(lambda r: httpx.Response(200, json=FOLDER if r.url.path.endswith("/filesFolder") else {"uploadUrl": DOWNLOAD}),
                lambda r: httpx.Response(202, json={"nextExpectedRanges": ["0-"]}))
    with pytest.raises(gf.GraphFileError, match="unexpected byte range"):
        await h.service.upload_channel_file(TEAM, CHANNEL, path)
    assert len(h.transfers) == 1


@pytest.mark.asyncio
async def test_ambiguous_small_upload_connection_is_not_retried(tmp_path):
    path = tmp_path / "small.txt"
    path.write_bytes(b"x")

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=FOLDER)
        raise httpx.ReadError("secret=" + DOWNLOAD)

    h = Harness(handler)
    with pytest.raises(gf.GraphFileError) as error:
        await h.service.upload_channel_file(TEAM, CHANNEL, path)
    assert "secret" not in str(error.value)
    assert len(h.requests) == 2


@pytest.mark.asyncio
async def test_empty_file_is_a_real_upload_and_missing_local_file_never_calls_graph(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    h = Harness(lambda r: httpx.Response(200 if r.method == "GET" else 201, json=FOLDER if r.method == "GET" else ITEM))
    await h.service.upload_channel_file(TEAM, CHANNEL, empty)
    assert h.requests[-1].content == b""
    with pytest.raises(gf.GraphFileError, match="could not be opened"):
        await h.service.upload_channel_file(TEAM, CHANNEL, tmp_path / "missing")
    assert len(h.requests) == 2


def test_profile_factory_uses_scoped_values_and_never_mixes_partial_identities(monkeypatch):
    module = types.ModuleType("tools.microsoft_graph_auth")
    module.GraphCredentials = lambda *values: values
    module.MicrosoftGraphTokenProvider = lambda credentials: credentials
    monkeypatch.setitem(sys.modules, "tools.microsoft_graph_auth", module)
    monkeypatch.setenv("MSGRAPH_CLIENT_SECRET", "wrong-global-secret")
    scoped = {"MSGRAPH_TENANT_ID": "tenant-a", "MSGRAPH_CLIENT_ID": "client-a", "MSGRAPH_CLIENT_SECRET": "secret-a"}
    service = gf.GraphFileClient.from_profile(lambda name, default: scoped.get(name, default), teams_credentials=("t", "c", "s"))
    assert service._token_provider == ("tenant-a", "client-a", "secret-a")
    other = gf.GraphFileClient.from_profile(lambda name, default: default, teams_credentials=("t2", "c2", "s2"))
    assert other._token_provider == ("t2", "c2", "s2")
    with pytest.raises(gf.GraphFileError, match="incomplete MSGRAPH"):
        gf.GraphFileClient.from_profile(lambda name, default: "partial" if name == "MSGRAPH_CLIENT_ID" else default,
                                       teams_credentials=("t", "c", "s"))


def test_filename_sanitization_cannot_turn_a_filename_into_a_graph_path():
    assert gf.safe_graph_filename("../../report:<bad>.txt") == "report__bad_.txt"
    assert gf.safe_graph_filename("..") == "file"
    with pytest.raises(gf.GraphFileError):
        gf._segment("../../other")
