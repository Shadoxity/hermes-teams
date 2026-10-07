"""Offline adapter-mixin tests: payload shapes, thread context and safe failures."""
import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_teams import files as files_module
from hermes_teams.graph_files import GraphFileError


TEAM = "11111111-2222-3333-4444-555555555555"
CHANNEL = "19:channel@thread.tacv2"
THREAD = CHANNEL + ";messageid=root"
FILE_URL = "https://contoso.sharepoint.com/sites/Example/Shared%20Documents/General/file.txt"
REFERENCE = {"contentType": "reference", "contentUrl": FILE_URL, "name": "file.txt"}


class SendResult(SimpleNamespace):
    def __init__(self, *, success=False, message_id=None, error=None, retryable=False, **kwargs):
        super().__init__(success=success, message_id=message_id, error=error, retryable=retryable, **kwargs)


@pytest.mark.asyncio
async def test_rich_card_is_one_activity_with_accessible_fallback(adapter):
    result = await adapter.send_rich_post(CHANNEL, {"template": "status", "title": "One card"})
    assert result.success
    payload = adapter._post_activity.await_args.args[1]
    assert not payload.get("text")
    assert "One card" in payload["summary"]
    assert "One card" in payload["attachments"][0]["content"]["fallbackText"]


class FakeAdapter(files_module.TeamsFilesMixin):
    def __init__(self):
        self._extra = {}
        self._app = object()
        self._conv_refs = {}
        self._file_targets = {CHANNEL: files_module.ChannelTarget(TEAM, CHANNEL)}
        self.client = SimpleNamespace(get_message_attachments=AsyncMock(return_value=[REFERENCE]),
                                      get_thread_attachments=AsyncMock(return_value=[REFERENCE]),
                                      download_attachment=AsyncMock(return_value=SimpleNamespace(
                                          data=b"actual downloaded bytes", name="file.txt", content_type="text/plain")),
                                      upload_channel_file=AsyncMock(return_value={"id": "file-id", "name": "file.txt", "webUrl": FILE_URL}))
        self._cache_attachment = AsyncMock(return_value=None)
        self._post_activity = AsyncMock(return_value=SimpleNamespace(id="posted"))
        self._send_file_consent = AsyncMock(return_value=SendResult(success=True, message_id="consent"))

    def _file_client(self):
        return self.client


@pytest.fixture
def adapter(monkeypatch):
    base = ModuleType("gateway.platforms.base")
    base.SendResult = SendResult
    base.cache_media_bytes_async = AsyncMock(return_value=SimpleNamespace(path="/cache/file.txt", media_type="text/plain", kind="document"))
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", base)
    instance = FakeAdapter()
    instance.cache = base.cache_media_bytes_async
    return instance


def activity(attachments, *, text="Please read the document", chat=THREAD, message="reply", reply="root", channel=True):
    return SimpleNamespace(attachments=attachments, text=text, id=message, reply_to_id=reply,
                           conversation=SimpleNamespace(id=chat, conversation_type="channel" if channel else "personal"),
                           channel_data={"team": {"aadGroupId": TEAM}, "channel": {"id": CHANNEL}} if channel else {})


def test_target_supports_dict_and_sdk_model_shapes():
    expected = files_module.ChannelTarget(TEAM, CHANNEL)
    assert files_module.extract_target(activity([])) == expected
    a = activity([])
    a.channel_data = SimpleNamespace(team=SimpleNamespace(aad_group_id=TEAM), channel=SimpleNamespace(id=CHANNEL))
    assert files_module.extract_target(a) == expected
    a.channel_data = {"team": {"id": TEAM}, "channel": {"id": CHANNEL}}
    assert files_module.extract_target(a) == expected
    a.channel_data["team"]["id"] = "19:teams-botframework-id@thread.skype"
    assert files_module.extract_target(a) is None


@pytest.mark.parametrize("chat", [THREAD, CHANNEL + ";messageId=root"])
def test_thread_root_preserves_original_thread_and_explicit_root_fallback(chat):
    assert files_module.thread_root(chat, "reply-id") == "root"
    assert files_module.thread_root(CHANNEL, "explicit-root") == "explicit-root"
    assert files_module.thread_root(CHANNEL) is None


@pytest.mark.asyncio
async def test_plain_html_body_mirror_hydrates_exact_reply_without_attachment_markup(adapter):
    a = activity([SimpleNamespace(content_type="text/html", content="<p>Please read the document</p>", content_url=None, name=None)])
    media, errors = await adapter._collect_teams_media(a)
    assert errors == []
    assert media == [("/cache/file.txt", "text/plain", "document")]
    adapter.client.get_message_attachments.assert_awaited_once_with(TEAM, CHANNEL, "reply", root_id="root")
    adapter.client.download_attachment.assert_awaited_once_with(REFERENCE, team_id=TEAM, channel_id=CHANNEL)
    adapter.cache.assert_awaited_once_with(b"actual downloaded bytes", filename="file.txt", mime_type="text/plain")


@pytest.mark.asyncio
async def test_file_download_info_without_preauthed_url_uses_graph_fallback(adapter):
    att = SimpleNamespace(content_type="application/vnd.microsoft.teams.file.download.info", content_url=FILE_URL,
                          content={"uniqueId": "file-guid", "fileType": "txt"}, name="file.txt")
    media, errors = await adapter._collect_teams_media(activity([att]))
    assert errors == [] and len(media) == 1
    adapter.client.get_message_attachments.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_reference_uses_file_service_without_redundant_graph_message_lookup(adapter):
    media, errors = await adapter._collect_teams_media(activity([REFERENCE]))
    assert len(media) == 1 and errors == []
    adapter.client.get_message_attachments.assert_not_awaited()
    adapter.client.download_attachment.assert_awaited_once()


@pytest.mark.asyncio
async def test_mixed_reference_and_incomplete_file_hydrates_only_missing_attachment(adapter):
    second_url = FILE_URL.replace("file.txt", "second.txt")
    second_reference = {**REFERENCE, "contentUrl": second_url, "name": "second.txt"}
    incomplete = {"contentType": "application/vnd.microsoft.teams.file.download.info",
                  "contentUrl": second_url, "name": "second.txt", "content": {"fileType": "txt"}}
    adapter.client.get_message_attachments.return_value = [REFERENCE, second_reference]
    media, errors = await adapter._collect_teams_media(activity([REFERENCE, incomplete]))
    assert len(media) == 2 and errors == []
    adapter.client.get_message_attachments.assert_awaited_once_with(TEAM, CHANNEL, "reply", root_id="root")
    downloaded = [call.args[0]["contentUrl"] for call in adapter.client.download_attachment.await_args_list]
    assert downloaded == [FILE_URL, second_url]  # Existing reference is not downloaded twice.


@pytest.mark.asyncio
async def test_direct_text_file_is_not_mistaken_for_html_body_mirror(adapter):
    adapter._cache_attachment.return_value = ("/cache/plain.txt", "text/plain", "document")
    direct = {"contentType": "text/plain", "contentUrl": "https://example.test/file.txt", "name": "file.txt"}
    media, errors = await adapter._collect_teams_media(activity([direct]))
    assert media == [("/cache/plain.txt", "text/plain", "document")] and errors == []
    adapter.client.get_message_attachments.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_cached_media_remains_supported_without_graph_lookup(adapter):
    adapter._cache_attachment.return_value = ("/cache/image.png", "image/png", "image")
    media, errors = await adapter._collect_teams_media(activity([{"contentType": "image/png", "contentUrl": "https://example.test/image.png"}]))
    assert media == [("/cache/image.png", "image/png", "image")] and errors == []
    adapter.client.get_message_attachments.assert_not_awaited()
    adapter.client.download_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_personal_reference_does_not_guess_channel_or_call_graph(adapter):
    media, errors = await adapter._collect_teams_media(activity([REFERENCE], channel=False))
    assert media == [] and errors
    adapter.client.get_message_attachments.assert_not_awaited()
    adapter.client.download_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_hydrated_file_failure_does_not_discard_other_files(adapter):
    adapter.client.get_message_attachments.return_value = [REFERENCE, {**REFERENCE, "contentUrl": FILE_URL + "2"}]
    adapter.client.download_attachment.side_effect = [GraphFileError("First file access denied"),
        SimpleNamespace(data=b"second file", name="second.txt", content_type="text/plain")]
    media, errors = await adapter._collect_teams_media(activity([{"contentType": "text/html", "content": "<attachment id='1'></attachment>"}]))
    assert len(media) == 1 and errors == ["First file access denied"]
    assert adapter.client.download_attachment.await_count == 2


@pytest.mark.asyncio
async def test_thread_download_does_not_fetch_redundant_or_invalid_current_message(adapter):
    result = await adapter.download_message_files(THREAD, "stale-reply", include_thread=True)
    adapter.client.get_message_attachments.assert_not_awaited()
    adapter.client.get_thread_attachments.assert_awaited_once_with(TEAM, CHANNEL, "root")
    assert result == [{"path": "/cache/file.txt", "mime_type": "text/plain", "kind": "document", "name": "file.txt"}]


@pytest.mark.asyncio
async def test_exact_download_preserves_reply_and_root_ids(adapter):
    await adapter.download_message_files(CHANNEL, "reply", root_id="root")
    adapter.client.get_message_attachments.assert_awaited_once_with(TEAM, CHANNEL, "reply", root_id="root")
    adapter.client.get_thread_attachments.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_upload_posts_link_in_original_thread(adapter):
    result = await adapter.send_document(THREAD, "/work/generated.txt", caption="Finished", file_name="output.txt")
    assert result.success and result.message_id == "posted"
    adapter.client.upload_channel_file.assert_awaited_once_with(TEAM, CHANNEL, "/work/generated.txt", filename="output.txt")
    chat, payload = adapter._post_activity.await_args.args
    assert chat == THREAD
    assert payload["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert payload["attachments"][0]["content"]["actions"][0]["url"] == FILE_URL


@pytest.mark.asyncio
async def test_post_failure_reports_existing_file_and_does_not_retry_upload(adapter):
    adapter._post_activity.side_effect = RuntimeError("transport broke with token=secret")
    result = await adapter.send_document(THREAD, "/work/generated.txt")
    assert not result.success and not result.retryable
    assert "File uploaded" in result.error and FILE_URL in result.error
    assert "secret" not in result.error
    adapter.client.upload_channel_file.assert_awaited_once()
    adapter._post_activity.assert_awaited_once()


@pytest.mark.asyncio
async def test_upload_denied_does_not_claim_or_post_delivery(adapter):
    adapter.client.upload_channel_file.side_effect = GraphFileError("File access denied")
    result = await adapter.send_document(THREAD, "/work/generated.txt")
    assert not result.success and result.error == "File access denied"
    adapter._post_activity.assert_not_awaited()


@pytest.mark.asyncio
async def test_personal_file_uses_consent_without_graph(adapter):
    adapter._conv_refs["personal"] = SimpleNamespace(conversation=SimpleNamespace(conversation_type="personal"))
    result = await adapter.send_document("personal", "/work/generated.txt", file_name="file.txt")
    assert result.success and result.message_id == "consent"
    adapter._send_file_consent.assert_awaited_once()
    adapter.client.upload_channel_file.assert_not_awaited()


def test_only_deliberately_safe_errors_are_exposed():
    assert files_module.TeamsFilesMixin._file_error(GraphFileError("Graph access denied")) == "Graph access denied"
    secret = "https://tenant.sharepoint.com/download?sig=SECRET"
    assert secret not in files_module.TeamsFilesMixin._file_error(ValueError(secret))
    assert "SECRET" not in files_module.TeamsFilesMixin._file_error(RuntimeError(secret))
    assert files_module.TeamsFilesMixin._file_error(files_module.FriendlyFileError("Known missing channel")) == "Known missing channel"


@pytest.mark.asyncio
async def test_supported_file_cache_failure_is_not_mistaken_for_download_success(adapter):
    adapter.cache.return_value = None
    media, errors = await adapter._collect_teams_media(activity([REFERENCE]))
    assert media == [] and errors


def test_in_memory_channel_mapping_survives_persistence_failure(adapter, tmp_path, monkeypatch):
    adapter._targets_file = tmp_path / "channels.json"
    adapter._file_targets = {}

    def cannot_replace(self, target):
        raise OSError("Read-only test storage")

    monkeypatch.setattr(type(adapter._targets_file), "replace", cannot_replace)
    adapter._remember_file_context(activity([]))
    assert adapter._file_target(THREAD) == files_module.ChannelTarget(TEAM, CHANNEL)


@pytest.fixture
def team_lookup_context(adapter, tmp_path):
    adapter._targets_file = tmp_path / "channels.json"
    adapter._file_targets = {}
    adapter._team_detail_cache = {}
    adapter._team_detail_inflight = {}
    a = activity([])
    a.channel_data = {"team": {"id": "19:parent-team@thread.tacv2"}, "channel": {"id": CHANNEL}}
    lookup = AsyncMock(return_value={"id": "19:parent-team@thread.tacv2", "aadGroupId": TEAM})
    return SimpleNamespace(activity=a, api=SimpleNamespace(teams=SimpleNamespace(get_by_id=lookup)))


@pytest.mark.asyncio
async def test_connector_team_lookup_resolves_and_persists_verified_group_id(adapter, team_lookup_context):
    ctx = team_lookup_context
    target = await adapter._ensure_file_context(ctx)
    assert target == files_module.ChannelTarget(TEAM, CHANNEL)
    ctx.api.teams.get_by_id.assert_awaited_once_with("19:parent-team@thread.tacv2")
    assert adapter._file_target(THREAD) == target
    assert adapter._targets_file.exists()
    # The original SDK activity is not mutated to insert guessed fields.
    assert "aadGroupId" not in ctx.activity.channel_data["team"]


@pytest.mark.asyncio
async def test_connector_team_lookup_accepts_installed_sdk_result_model_fields(adapter, team_lookup_context):
    ctx = team_lookup_context
    ctx.api.teams.get_by_id.return_value = SimpleNamespace(id="19:parent-team@thread.tacv2", aad_group_id=TEAM)
    assert await adapter._ensure_file_context(ctx) == files_module.ChannelTarget(TEAM, CHANNEL)


@pytest.mark.asyncio
async def test_existing_activity_guid_or_persisted_route_needs_no_connector_lookup(adapter, team_lookup_context):
    ctx = team_lookup_context
    ctx.activity.channel_data["team"]["aadGroupId"] = TEAM
    assert await adapter._ensure_file_context(ctx) == files_module.ChannelTarget(TEAM, CHANNEL)
    del ctx.activity.channel_data["team"]["aadGroupId"]
    assert await adapter._ensure_file_context(ctx) == files_module.ChannelTarget(TEAM, CHANNEL)
    ctx.api.teams.get_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_lookup_result_is_available_to_current_message_media_collection(adapter, team_lookup_context):
    ctx = team_lookup_context
    ctx.activity.attachments = [REFERENCE]
    await adapter._ensure_file_context(ctx)
    media, errors = await adapter._collect_teams_media(ctx.activity)
    assert errors == [] and len(media) == 1
    adapter.client.download_attachment.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_team_lookups_share_one_sdk_request(adapter, team_lookup_context):
    ctx = team_lookup_context
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_lookup(team_id):
        started.set()
        await release.wait()
        return {"id": team_id, "aadGroupId": TEAM}

    ctx.api.teams.get_by_id.side_effect = slow_lookup
    first = asyncio.create_task(adapter._ensure_file_context(ctx))
    await started.wait()
    second = asyncio.create_task(adapter._ensure_file_context(ctx))
    await asyncio.sleep(0)
    release.set()
    result = await asyncio.gather(first, second)
    assert result == [files_module.ChannelTarget(TEAM, CHANNEL)] * 2
    ctx.api.teams.get_by_id.assert_awaited_once()
    assert adapter._team_detail_inflight == {}


@pytest.mark.asyncio
async def test_positive_team_cache_serves_another_channel_in_same_team(adapter, team_lookup_context):
    ctx = team_lookup_context
    await adapter._ensure_file_context(ctx)
    ctx.activity.conversation.id = "19:second-channel@thread.tacv2;messageid=thread2"
    ctx.activity.channel_data["channel"]["id"] = "19:second-channel@thread.tacv2"
    target = await adapter._ensure_file_context(ctx)
    assert target == files_module.ChannelTarget(TEAM, "19:second-channel@thread.tacv2")
    ctx.api.teams.get_by_id.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{"aadGroupId": "19:not-a-group-guid"}, {"aadGroupId": "../unsafe"},
                                    {"aadGroupId": TEAM, "id": "19:unexpected-team"}, {}])
async def test_invalid_or_mismatched_team_details_never_create_mapping(adapter, team_lookup_context, result):
    ctx = team_lookup_context
    ctx.api.teams.get_by_id.return_value = result
    assert await adapter._ensure_file_context(ctx) is None
    assert adapter._file_targets == {} and not adapter._targets_file.exists()
    assert await adapter._ensure_file_context(ctx) is None
    ctx.api.teams.get_by_id.assert_awaited_once()  # Negative result is cooled down too.


@pytest.mark.asyncio
async def test_sdk_failure_is_nonfatal_redacted_and_cooled_down(adapter, team_lookup_context, caplog, monkeypatch):
    ctx = team_lookup_context
    now = [100.0]
    monkeypatch.setattr(files_module.time, "monotonic", lambda: now[0])
    ctx.api.teams.get_by_id.side_effect = RuntimeError("Authorization: Bearer SECRET")
    assert await adapter._ensure_file_context(ctx) is None
    assert await adapter._ensure_file_context(ctx) is None
    ctx.api.teams.get_by_id.assert_awaited_once()
    assert "SECRET" not in caplog.text
    now[0] += files_module._TEAM_LOOKUP_FAILURE_TTL + 1
    assert await adapter._ensure_file_context(ctx) is None
    assert ctx.api.teams.get_by_id.await_count == 2
    assert adapter._file_targets == {}


@pytest.mark.asyncio
async def test_sdk_lookup_timeout_is_bounded_and_cleans_inflight_cache(adapter, team_lookup_context, monkeypatch):
    ctx = team_lookup_context
    monkeypatch.setattr(files_module, "_TEAM_LOOKUP_TIMEOUT_SECONDS", 0.01)
    cancelled = asyncio.Event()

    async def never_finishes(team_id):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    ctx.api.teams.get_by_id.side_effect = never_finishes
    assert await adapter._ensure_file_context(ctx) is None
    assert cancelled.is_set() and adapter._team_detail_inflight == {}
    assert await adapter._ensure_file_context(ctx) is None
    ctx.api.teams.get_by_id.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("team_id", ["https://evil.test/team", "../team", "team?token=x", "team\n", "", ".", ".."])
async def test_only_safe_exact_activity_team_identifier_reaches_sdk(adapter, team_lookup_context, team_id):
    ctx = team_lookup_context
    ctx.activity.channel_data["team"]["id"] = team_id
    assert await adapter._ensure_file_context(ctx) is None
    ctx.api.teams.get_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_channel_activity_never_performs_team_details_lookup(adapter, team_lookup_context):
    ctx = team_lookup_context
    ctx.activity.conversation.conversation_type = "personal"
    assert await adapter._ensure_file_context(ctx) is None
    ctx.api.teams.get_by_id.assert_not_awaited()
