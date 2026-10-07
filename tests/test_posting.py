"""Posting contracts based on Microsoft's CreateConversation channel-thread API."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from hermes_teams import posting

CHANNEL = "19:channel@thread.tacv2"
THREAD = CHANNEL + ";messageid=42"
URL = "https://smba.trafficmanager.net/teams/v3/conversations"


class Adapter(posting.TeamsPostingMixin):
    def __init__(self):
        self._conv_refs = {}
        self._file_targets = {}
        self._tenant_id = "test-tenant"
        self._get_botframework_token = AsyncMock(return_value="test-token")
        self.sent = []

    def _transport_service_url_for(self, chat_id):
        return "https://smba.trafficmanager.net/teams/"

    def _remember_sent(self, receipt):
        self.sent.append(receipt["id"])


@pytest.fixture
def adapter():
    return Adapter()


def install_http(monkeypatch, *, status=201, data=None, body=None, error=None):
    calls, options = [], []
    real_client = httpx.AsyncClient
    def respond(request):
        calls.append(request)
        if error:
            raise error
        if body is not None:
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=data)
    def client(**kwargs):
        options.append(kwargs)
        return real_client(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(posting.httpx, "AsyncClient", client)
    monkeypatch.setattr(posting, "_trust_env", lambda: False)
    return calls, options


@pytest.mark.parametrize("chat,reply,expected", [
    (CHANNEL, None, True), (THREAD, None, False), (CHANNEL, "42", False),
    (CHANNEL + ";messageId=42", None, False), ("a:personal", None, False),
    ("19:group@thread.v2", None, False),
])
def test_only_unthreaded_channels_create_conversations(adapter, chat, reply, expected):
    assert adapter._is_new_channel_post(chat, reply) is expected


def test_known_legacy_channel_can_create_but_known_group_chat_cannot(adapter):
    legacy = "19:channel@thread.skype"
    adapter._file_targets[legacy] = object()
    assert adapter._is_new_channel_post(legacy)
    adapter._conv_refs[legacy] = SimpleNamespace(conversation=SimpleNamespace(conversation_type="groupChat"))
    assert not adapter._is_new_channel_post(legacy)
    adapter._conv_refs[legacy] = {"conversation": {"conversationType": "channel"}}
    assert adapter._is_new_channel_post(legacy)


@pytest.mark.asyncio
async def test_root_channel_create_posts_activity_once_and_normalizes_receipt(adapter, monkeypatch):
    import json
    calls, options = install_http(monkeypatch, data={"id": THREAD, "activityId": "42"})
    activity = {"type": "message", "text": "Ready", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": {"type": "AdaptiveCard"}}]}
    result = await adapter._create_channel_post(CHANNEL, activity)
    assert result == {"id": "42", "conversation_id": THREAD}
    assert adapter.sent == ["42"] and len(calls) == 1
    assert str(calls[0].url) == URL
    assert json.loads(calls[0].content) == {"isGroup": True, "tenantId": "test-tenant", "channelData": {"channel": {"id": CHANNEL}}, "activity": activity}
    assert calls[0].headers["Authorization"] == "Bearer test-token"
    assert options[0]["follow_redirects"] is False and options[0]["trust_env"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 201])
async def test_existing_activity_receipt_preserves_message_id(adapter, monkeypatch, status):
    calls, _ = install_http(monkeypatch, status=status, data={"id": "reply-99"})
    result = await adapter._request_activity_json(URL + "/thread/activities", {"type": "message"})
    assert result == {"id": "reply-99"} and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [202, 204])
async def test_empty_accepted_response_is_ambiguous_and_never_retried(adapter, monkeypatch, status):
    calls, _ = install_http(monkeypatch, status=status, body=b"")
    with pytest.raises(posting.PostingError, match="without a confirmed delivery receipt"):
        await adapter._create_channel_post(CHANNEL, {"type": "message", "text": "Ready"})
    assert len(calls) == 1 and adapter.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 403, 404, 429, 500])
async def test_errors_are_safe_bounded_and_do_not_retry_root_posts(adapter, monkeypatch, status):
    calls, _ = install_http(monkeypatch, status=status, data={"error": "test-token-secret"})
    with pytest.raises(posting.PostingError) as failure:
        await adapter._create_channel_post(CHANNEL, {"type": "message"})
    assert f"HTTP {status}" in str(failure.value)
    assert "test-token" not in str(failure.value)
    assert len(calls) == 1 and adapter.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [
    {}, {"id": THREAD}, {"id": THREAD, "activityId": "43"},
    {"id": "19:other@thread.tacv2;messageid=42", "activityId": "42"},
    {"id": "19:channel@thread.tacv2", "activityId": "42"}, [],
])
async def test_incomplete_or_mismatched_create_receipt_is_not_delivery(adapter, monkeypatch, receipt):
    calls, _ = install_http(monkeypatch, data=receipt)
    with pytest.raises(posting.PostingError):
        await adapter._create_channel_post(CHANNEL, {"type": "message"})
    assert len(calls) == 1 and not adapter.sent


@pytest.mark.asyncio
async def test_malformed_success_body_is_honest_and_never_exposes_raw_body(adapter, monkeypatch):
    calls, _ = install_http(monkeypatch, status=200, body=b"sensitive non-json response")
    with pytest.raises(posting.PostingError) as failure:
        await adapter._create_channel_post(CHANNEL, {"type": "message"})
    assert "unconfirmed" in str(failure.value) and "sensitive" not in str(failure.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_timeout_leaves_outcome_unknown_without_an_automatic_second_post(adapter, monkeypatch):
    calls, _ = install_http(monkeypatch, error=httpx.ReadTimeout("sensitive-url-token"))
    with pytest.raises(posting.PostingError, match="could not be confirmed") as failure:
        await adapter._create_channel_post(CHANNEL, {"type": "message"})
    assert "sensitive-url" not in str(failure.value) and len(calls) == 1


@pytest.mark.asyncio
async def test_unsafe_connector_is_rejected_before_token_acquisition(adapter):
    with pytest.raises(posting.PostingError):
        await adapter._request_activity_json("https://evil.test/v3/conversations", {})
    adapter._get_botframework_token.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("chat,payload", [(THREAD, {"type": "message"}), (CHANNEL, {"type": "message", "replyToId": "42"})])
async def test_create_helper_refuses_thread_inputs_before_post(adapter, chat, payload):
    adapter._request_activity_json = AsyncMock()
    with pytest.raises(posting.PostingError):
        await adapter._create_channel_post(chat, payload)
    adapter._request_activity_json.assert_not_awaited()
