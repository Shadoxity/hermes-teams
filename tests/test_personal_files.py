"""Consent ownership, bounded storage, and no-credential upload regressions."""
import asyncio
from enum import Enum
from types import ModuleType, SimpleNamespace
import sys
from unittest.mock import AsyncMock

import pytest

import personal_files as personal


class Attachment:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Activity:
    def __init__(self):
        self.attachments = []

    def add_attachments(self, attachment):
        self.attachments.append(attachment)
        return self


class Adapter(personal.PersonalFilesMixin):
    def __init__(self):
        self._app = object()
        self._extra = {}
        self._conv_refs = {"chat-1": SimpleNamespace(
            conversation=SimpleNamespace(id="chat-1", conversation_type="personal"),
            user=SimpleNamespace(id="29:user", aad_object_id="aad-user"),
        )}
        self.sent = []
        self._init_personal_files()
        self._card_action_denied = lambda actor: None
        self.send = AsyncMock()
        self.delete_message = AsyncMock()
        self._upload_consented_file = AsyncMock()

    async def _send_via_conv_ref(self, chat_id, activity, fallback):
        self.sent.append((chat_id, activity))
        return SimpleNamespace(id=f"card-{len(self.sent)}")


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(personal, "_send_result", lambda **kwargs: SimpleNamespace(**kwargs))
    api = ModuleType("microsoft_teams.api")
    api.Attachment, api.MessageActivityInput = Attachment, Activity
    monkeypatch.setitem(sys.modules, "microsoft_teams.api", api)
    safety = ModuleType("tools.url_safety")
    safety.is_safe_url = lambda url: True
    monkeypatch.setitem(sys.modules, "tools.url_safety", safety)
    instance = Adapter()
    yield instance
    for file_id in list(instance._personal_pending):
        instance._drop_personal_pending(file_id)


async def offer(adapter, tmp_path, data=b"file bytes", **kwargs):
    path = tmp_path / "report.pdf"
    path.write_bytes(data)
    result = await adapter._send_file_consent("chat-1", str(path), **kwargs)
    assert result.success
    file_id = adapter.sent[-1][1].attachments[0].content["acceptContext"]["file_id"]
    return file_id


def context(file_id, *, action="accept", chat="chat-1", user="29:user", aad="aad-user", card="card-1"):
    return SimpleNamespace(activity=SimpleNamespace(
        conversation=SimpleNamespace(id=chat, conversation_type="personal"),
        from_=SimpleNamespace(id=user, aad_object_id=aad),
        reply_to_id=card,
        value={"action": action, "context": {"file_id": file_id}, "uploadInfo": {
            "uploadUrl": "https://tenant.sharepoint.com/upload?sig=PRIVATE",
            "contentUrl": "https://tenant.sharepoint.com/personal/user/report.pdf?web=1",
            "uniqueId": "drive-item-id", "fileType": "pdf", "name": "report.pdf",
        }},
    ))


@pytest.mark.asyncio
async def test_offer_is_bound_and_context_contains_only_opaque_id(adapter, tmp_path):
    file_id = await offer(adapter, tmp_path, caption="Monthly report")
    card = adapter.sent[0][1].attachments[0]
    assert card.content_type == personal._CONSENT_TYPE
    assert card.content["description"] == "Monthly report"
    assert card.content["sizeInBytes"] == len(b"file bytes")
    assert card.content["acceptContext"] == card.content["declineContext"] == {"file_id": file_id}
    pending = adapter._personal_pending[file_id]
    assert pending.chat_id == "chat-1"
    assert pending.recipient == {"id": "29:user", "aad": "aad-user"}
    assert pending.card_id == "card-1"
    assert pending.timer is not None
    assert adapter._personal_pending_bytes == len(b"file bytes")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"chat": "chat-2"}, {"user": "29:another"}, {"aad": "another-aad"},
    {"aad": None}, {"card": "another-card"}, {"card": None},
])
@pytest.mark.parametrize("action", ["accept", "decline"])
async def test_cross_chat_recipient_or_card_cannot_consume_offer(adapter, tmp_path, change, action):
    file_id = await offer(adapter, tmp_path)
    await adapter._on_file_consent(context(file_id, action=action, **change))
    assert file_id in adapter._personal_pending
    adapter._upload_consented_file.assert_not_awaited()
    adapter.delete_message.assert_not_awaited()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_denied_actor_cannot_consume_or_dismiss_offer(adapter, tmp_path):
    file_id = await offer(adapter, tmp_path)
    adapter._card_action_denied = lambda actor: "Not authorized"
    await adapter._on_file_consent(context(file_id))
    assert file_id in adapter._personal_pending
    adapter._upload_consented_file.assert_not_awaited()
    adapter.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_accept_uploads_exact_bytes_once_and_preserves_final_file_url(adapter, tmp_path):
    data = b"%PDF-1.7 binary\x00\xff"
    file_id = await offer(adapter, tmp_path, data)
    ctx = context(file_id)
    await asyncio.gather(adapter._on_file_consent(ctx), adapter._on_file_consent(ctx))
    adapter._upload_consented_file.assert_awaited_once_with(ctx.activity.value["uploadInfo"]["uploadUrl"], data)
    assert not adapter._personal_pending
    assert adapter._personal_pending_bytes == 0
    card = adapter.sent[-1][1].attachments[0]
    assert card.content_type == personal._INFO_TYPE
    assert card.content_url == ctx.activity.value["uploadInfo"]["contentUrl"]
    assert card.content == {"uniqueId": "drive-item-id", "fileType": "pdf"}
    adapter.delete_message.assert_awaited_once_with("chat-1", "card-1")


@pytest.mark.asyncio
async def test_accept_understands_sdk_enums_and_camel_case_models(adapter, tmp_path):
    class Action(str, Enum):
        ACCEPT = "accept"
    file_id = await offer(adapter, tmp_path)
    ctx = context(file_id)
    payload = ctx.activity.value
    payload["action"] = Action.ACCEPT
    payload["context"] = SimpleNamespace(fileId=file_id)
    ctx.activity.value = SimpleNamespace(**payload)
    ctx.activity.replyToId = ctx.activity.reply_to_id
    ctx.activity.reply_to_id = None
    await adapter._on_file_consent(ctx)
    adapter._upload_consented_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_decline_discards_bytes_and_dismisses_original_card(adapter, tmp_path):
    file_id = await offer(adapter, tmp_path)
    await adapter._on_file_consent(context(file_id, action="decline"))
    assert not adapter._personal_pending and adapter._personal_pending_bytes == 0
    adapter._upload_consented_file.assert_not_awaited()
    adapter.send.assert_awaited_once_with("chat-1", "File upload declined.")
    adapter.delete_message.assert_awaited_once_with("chat-1", "card-1")


@pytest.mark.asyncio
async def test_expired_offer_never_uploads(adapter, tmp_path):
    file_id = await offer(adapter, tmp_path)
    adapter._personal_pending[file_id].expires_at = 0
    await adapter._on_file_consent(context(file_id))
    assert not adapter._personal_pending and adapter._personal_pending_bytes == 0
    adapter._upload_consented_file.assert_not_awaited()
    assert "expired" in adapter.send.await_args.args[1]


@pytest.mark.asyncio
async def test_expiry_timer_releases_bytes_without_further_requests(adapter, tmp_path):
    adapter._extra["personal_file_consent_ttl_seconds"] = 1
    await offer(adapter, tmp_path)
    await asyncio.sleep(1.05)
    assert not adapter._personal_pending and adapter._personal_pending_bytes == 0


@pytest.mark.asyncio
async def test_unknown_or_replayed_id_does_not_delete_supplied_message(adapter):
    await adapter._on_file_consent(context("unknown", card="sensitive-message"))
    adapter.delete_message.assert_not_awaited()
    adapter._upload_consented_file.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,url", [
    ("uploadUrl", "http://tenant.sharepoint.com/upload"),
    ("uploadUrl", "https://tenant.sharepoint.com.attacker.test/upload"),
    ("uploadUrl", "https://user:password@tenant.sharepoint.com/upload"),
    ("uploadUrl", "https://127.0.0.1/upload"),
    ("uploadUrl", "https://tenant.sharepoint.com:8443/upload"),
    ("contentUrl", "https://tenant.sharepoint.com/report?access_token=SECRET"),
    ("contentUrl", "https://attacker.test/report.pdf"),
])
async def test_unsafe_upload_or_file_info_urls_do_not_consume_offer(adapter, tmp_path, field, url):
    file_id = await offer(adapter, tmp_path)
    ctx = context(file_id)
    ctx.activity.value["uploadInfo"][field] = url
    await adapter._on_file_consent(ctx)
    assert file_id in adapter._personal_pending
    adapter._upload_consented_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_ssrf_rejection_precedes_grant_consumption(adapter, tmp_path, monkeypatch):
    file_id = await offer(adapter, tmp_path)
    monkeypatch.setattr(sys.modules["tools.url_safety"], "is_safe_url", lambda url: False)
    await adapter._on_file_consent(context(file_id))
    assert file_id in adapter._personal_pending
    adapter._upload_consented_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_failure_is_sanitized_and_cannot_replay(adapter, tmp_path, caplog):
    file_id = await offer(adapter, tmp_path)
    adapter._upload_consented_file.side_effect = RuntimeError("https://tenant.sharepoint.com/?sig=SUPERSECRET")
    await adapter._on_file_consent(context(file_id))
    await adapter._on_file_consent(context(file_id))
    adapter._upload_consented_file.assert_awaited_once()
    assert not adapter._personal_pending
    assert "SUPERSECRET" not in caplog.text
    assert "SUPERSECRET" not in adapter.send.await_args.args[1]


@pytest.mark.asyncio
async def test_confirmation_failure_does_not_claim_upload_failed(adapter, tmp_path):
    file_id = await offer(adapter, tmp_path)
    adapter._send_file_info_card = AsyncMock(side_effect=RuntimeError("Failure"))
    await adapter._on_file_consent(context(file_id))
    assert "file uploaded" in adapter.send.await_args.args[1].lower()
    adapter.delete_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["no_reference", "group_chat", "no_recipient"])
async def test_offers_require_known_personal_recipient(adapter, tmp_path, mode):
    path = tmp_path / "file.txt"
    path.write_bytes(b"data")
    if mode == "no_reference":
        adapter._conv_refs.clear()
    elif mode == "group_chat":
        adapter._conv_refs["chat-1"].conversation.conversation_type = "groupChat"
    else:
        adapter._conv_refs["chat-1"].user = None
    result = await adapter._send_file_consent("chat-1", str(path))
    assert result.success is False and not adapter._personal_pending
    assert not adapter.sent


@pytest.mark.asyncio
async def test_file_count_and_total_byte_limits_preserve_existing_offers(adapter, tmp_path):
    adapter._extra.update(max_file_bytes=8, personal_pending_files=2, personal_pending_bytes=10)
    first = await offer(adapter, tmp_path, b"123456")
    path = tmp_path / "second.txt"
    path.write_bytes(b"12345")
    result = await adapter._send_file_consent("chat-1", str(path))
    assert not result.success
    assert first in adapter._personal_pending
    assert adapter._personal_pending_bytes == 6
    path.write_bytes(b"1234")
    result = await adapter._send_file_consent("chat-1", str(path))
    assert result.success and adapter._personal_pending_bytes == 10
    result = await adapter._send_file_consent("chat-1", str(path))
    assert not result.success and "Too many" in result.error


@pytest.mark.asyncio
async def test_bytes_being_uploaded_still_count_toward_memory_budget(adapter, tmp_path):
    adapter._extra.update(max_file_bytes=8, personal_pending_bytes=8)
    file_id = await offer(adapter, tmp_path, b"123456")
    started, finish = asyncio.Event(), asyncio.Event()
    async def slow_upload(url, data):
        started.set()
        await finish.wait()
    adapter._upload_consented_file.side_effect = slow_upload
    task = asyncio.create_task(adapter._on_file_consent(context(file_id)))
    await started.wait()
    try:
        path = tmp_path / "another.txt"
        path.write_bytes(b"1234")
        result = await adapter._send_file_consent("chat-1", str(path))
        assert not result.success
        assert adapter._personal_pending_bytes == 0
        assert adapter._personal_inflight_bytes == 6
    finally:
        finish.set()
        await task
    assert adapter._personal_inflight_bytes == 0
    assert adapter._personal_inflight_count == 0


@pytest.mark.asyncio
async def test_oversize_and_empty_file_rejected_without_sdk_send(adapter, tmp_path):
    adapter._extra["max_file_bytes"] = 4
    path = tmp_path / "file.txt"
    for data in (b"12345", b""):
        path.write_bytes(data)
        result = await adapter._send_file_consent("chat-1", str(path))
        assert not result.success and not adapter.sent


@pytest.mark.asyncio
async def test_send_failure_releases_bytes_and_hides_exception_details(adapter, tmp_path, caplog):
    adapter._send_via_conv_ref = AsyncMock(side_effect=RuntimeError("TOKEN=secret"))
    path = tmp_path / "report.txt"
    path.write_bytes(b"content")
    result = await adapter._send_file_consent("chat-1", str(path))
    assert not result.success
    assert not adapter._personal_pending and adapter._personal_pending_bytes == 0
    assert "secret" not in result.error and "secret" not in caplog.text


def test_limits_use_instance_config_and_have_absolute_caps(adapter):
    adapter._extra.update(max_file_bytes=10**12, personal_pending_bytes=10**12,
                          personal_pending_files=999, personal_file_consent_ttl_seconds=999999)
    assert adapter._personal_limits() == (50 * 1024**2, 1800, 32, 80 * 1024**2)


class UploadResponse:
    def __init__(self, status):
        self.status_code = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class UploadClient:
    def __init__(self, status=201):
        self.status = status
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def stream(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return UploadResponse(self.status)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 201, 202, 302, 401, 500])
async def test_real_upload_helper_disables_redirects_omits_auth_and_requires_completion(adapter, monkeypatch, status):
    client = UploadClient(status)
    options = {}
    def make_client(**kwargs):
        options.update(kwargs)
        return client
    monkeypatch.setattr(sys.modules["tools.url_safety"], "create_ssrf_safe_async_client", make_client, raising=False)
    upload = personal.PersonalFilesMixin._upload_consented_file
    url = "https://tenant.sharepoint.com/upload?sig=SECRET"
    if status in (200, 201):
        await upload(adapter, url, b"abc")
    else:
        with pytest.raises(ValueError, match="completion"):
            await upload(adapter, url, b"abc")
    assert options == {"timeout": 60.0, "follow_redirects": False}
    method, requested_url, kwargs = client.requests[0]
    assert (method, requested_url) == ("PUT", url)
    assert kwargs["content"] == b"abc"
    assert kwargs["headers"] == {"Content-Type": "application/octet-stream", "Content-Length": "3",
                                  "Content-Range": "bytes 0-2/3"}
