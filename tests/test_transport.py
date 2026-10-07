"""Network-free regression tests for the custom transport and Teams thread routing."""
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

_SPEC = importlib.util.spec_from_file_location("teams_transport_under_test", Path(__file__).parents[1] / "transport.py")
transport = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = transport
_SPEC.loader.exec_module(transport)


class FakeAdapter(transport.TeamsTransportMixin):
    MAX_MESSAGE_LENGTH = 100

    def __init__(self):
        self._app = SimpleNamespace(id="bot-id")
        self._extra = {"stream_edit_interval": 0}
        self._conv_refs = {}
        self._tenant_id = "tenant-id"
        self._init_teams_transport()
        self._update_activity = AsyncMock()
        self.send = AsyncMock()
        self._reaction_handler = AsyncMock()
        self._platform_event_handler = AsyncMock()

    def format_message(self, text):
        return text

    def build_source(self, **kwargs):
        return SimpleNamespace(**kwargs)


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(transport, "_send_result", lambda **kwargs: SimpleNamespace(**kwargs))
    return FakeAdapter()


def http_error(status, retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
    response = httpx.Response(status, headers=headers, request=httpx.Request("PUT", "https://example.test"))
    return httpx.HTTPStatusError("sensitive raw response text", request=response.request, response=response)


@pytest.mark.parametrize("chat_id", ["19:abc@thread.tacv2;messageid=42", "19:abc@thread.tacv2;messageId=42"])
def test_flattening_is_for_connector_address_only(chat_id):
    assert transport._flat_conversation_id(chat_id) == "19:abc@thread.tacv2"
    assert ";messageid" not in transport._bf_activity_url(transport._DEFAULT_SERVICE_URL, chat_id, "123")


@pytest.mark.parametrize("service_url", [
    "http://smba.trafficmanager.net/teams/", "https://attacker.trafficmanager.net/teams/",
    "https://smba.trafficmanager.net.attacker.test/teams/", "https://smba.trafficmanager.net:8443/",
    "https://user:pass@smba.trafficmanager.net/", "https://smba.trafficmanager.net/?token=secret",
    "https://smba.trafficmanager.net/#fragment",
])
def test_connector_url_rejects_unsafe_hosts_credentials_ports_queries(service_url):
    with pytest.raises(ValueError):
        transport._bf_activity_url(service_url, "19:thread", "123")


@pytest.mark.parametrize("chat_id, activity_id", [("19:a/../../", "1"), ("19:abc", "../123"), ("", "1"), ("19:abc", "1\n")])
def test_connector_identifiers_reject_path_injection(chat_id, activity_id):
    with pytest.raises(ValueError):
        transport._bf_activity_url(transport._DEFAULT_SERVICE_URL, chat_id, activity_id)


def test_thread_reference_selects_region_before_flattening(adapter):
    chat_id = "19:abc@thread.tacv2;messageid=42"
    adapter._conv_refs[chat_id] = SimpleNamespace(service_url="https://smba.trafficmanager.net/emea/")
    assert "/emea/" in adapter._transport_service_url_for(chat_id)


@pytest.mark.asyncio
async def test_updates_and_finalization_preserve_activity_without_extra_posts(adapter):
    chat = "19:abc@thread.tacv2;messageid=42"
    first = await adapter.edit_message(chat, "123", "hello")
    duplicate = await adapter.edit_message(chat, "123", "hello")
    final = await adapter.edit_message(chat, "123", "hello world", finalize=True)
    assert first.success and duplicate.success and final.success
    assert adapter._update_activity.await_count == 2
    adapter._update_activity.assert_awaited_with(chat, "123", "hello world")
    adapter.send.assert_not_awaited()
    assert adapter.draft_stream_is_message is False


@pytest.mark.asyncio
async def test_throttled_concurrent_edits_coalesce_to_newest_content(adapter, monkeypatch):
    await adapter.edit_message("19:abc", "123", "start")
    adapter._extra["stream_edit_interval"] = 1
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause(delay):
        entered.set()
        await resume.wait()

    monkeypatch.setattr(transport.asyncio, "sleep", pause)
    second = asyncio.create_task(adapter.edit_message("19:abc", "123", "middle"))
    await entered.wait()
    final_started = asyncio.Event()

    async def finalize():
        final_started.set()
        return await adapter.edit_message("19:abc", "123", "finished", finalize=True)

    final = asyncio.create_task(finalize())
    await final_started.wait()
    resume.set()
    results = await asyncio.gather(second, final)
    assert all(r.success for r in results)
    assert adapter._update_activity.await_count == 2
    adapter._update_activity.assert_awaited_with("19:abc", "123", "finished")


@pytest.mark.asyncio
async def test_unicode_preview_obeys_byte_limit_final_never_silently_truncates(adapter):
    preview = await adapter.edit_message("19:abc", "123", "😀" * 40)
    assert preview.success
    assert len(adapter._update_activity.await_args.args[2].encode("utf-8")) <= 100
    final = await adapter.edit_message("19:abc", "123", "😀" * 40, finalize=True)
    assert final.success is False
    assert "byte limit" in final.error
    assert adapter._update_activity.await_count == 1
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 405, 501])
async def test_unsupported_updates_return_failure_without_duplicate_send(adapter, status):
    adapter._update_activity.side_effect = http_error(status)
    result = await adapter.edit_message("19:abc", "123", "content")
    assert result.success is False
    assert "sensitive" not in result.error
    adapter._update_activity.assert_awaited_once()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_short_rate_limit_retried_once(adapter):
    adapter._update_activity.side_effect = [http_error(429, 0), None]
    result = await adapter.edit_message("19:abc", "123", "content")
    assert result.success and adapter._update_activity.await_count == 2


@pytest.mark.asyncio
async def test_second_rate_limit_returns_retry_metadata_without_loop(adapter):
    adapter._update_activity.side_effect = [http_error(429, 0), http_error(429, 7)]
    result = await adapter.edit_message("19:abc", "123", "content")
    assert not result.success and result.retryable and result.retry_after == 7
    assert result.error_kind == "rate_limited"
    assert adapter._update_activity.await_count == 2


@pytest.mark.asyncio
async def test_long_rate_limit_does_not_sleep_or_post_fallback(adapter, monkeypatch):
    pause = AsyncMock()
    monkeypatch.setattr(transport.asyncio, "sleep", pause)
    adapter._update_activity.side_effect = http_error(429, 30)
    result = await adapter.edit_message("19:abc", "123", "content")
    assert not result.success and result.retry_after == 30
    pause.assert_not_awaited()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_missing_preview_is_idempotent(adapter):
    adapter._delete_activity_via_rest = AsyncMock(side_effect=http_error(404))
    assert await adapter.delete_message("19:abc", "123") is True
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_failed_preview_returns_false(adapter):
    adapter._delete_activity_via_rest = AsyncMock(side_effect=http_error(403))
    assert await adapter.delete_message("19:abc", "123") is False


@pytest.mark.asyncio
async def test_connector_request_uses_thread_reference_and_never_follows_redirects(adapter, monkeypatch):
    captured = {}
    chat = "19:abc@thread.tacv2;messageid=42"
    adapter._conv_refs[chat] = SimpleNamespace(service_url="https://smba.trafficmanager.net/emea/")
    adapter._get_botframework_token = AsyncMock(return_value="test-token")
    base = sys.modules.get("gateway.platforms.base")
    if base is None:
        base = SimpleNamespace(gateway_trust_env=lambda: False)
        monkeypatch.setitem(sys.modules, "gateway.platforms.base", base)
    else:
        monkeypatch.setattr(base, "gateway_trust_env", lambda: False, raising=False)

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def request(self, method, url, **kwargs):
            captured.update(method=method, url=url, **kwargs)
            return httpx.Response(200, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    await adapter._connector_request("PUT", chat, "123", payload={"text": "hi"})
    assert captured["url"] == "https://smba.trafficmanager.net/emea/v3/conversations/19:abc@thread.tacv2/activities/123"
    assert captured["client"]["follow_redirects"] is False
    assert captured["headers"] == {"Authorization": "Bearer test-token"}


@pytest.mark.asyncio
async def test_invalid_connector_rejected_before_token_acquisition(adapter):
    adapter._extra["service_url"] = "https://evil.test/"
    adapter._get_botframework_token = AsyncMock()
    with pytest.raises(ValueError):
        await adapter._connector_request("PUT", "19:abc", "123")
    adapter._get_botframework_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaction_default_target_is_exact_thread_scoped_and_removal_remembers_type(adapter):
    chat = "19:abc;messageid=42"
    adapter._connector_request = AsyncMock()
    adapter._remember_inbound_activity(SimpleNamespace(id="99", conversation=SimpleNamespace(id=chat)))
    missing = await adapter.add_reaction("19:abc;messageid=43", "👍")
    assert not missing["success"]
    added = await adapter.add_reaction(chat, ":thumbsup:")
    assert added == {"success": True, "message_id": "99", "reaction": "like"}
    adapter._connector_request.assert_awaited_with("PUT", chat, "99", reaction="like")
    removed = await adapter.remove_reaction(chat)
    assert removed["success"]
    adapter._connector_request.assert_awaited_with("DELETE", chat, "99", reaction="like")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, expected", [("success", "2705_whiteheavycheckmark"), ("failure", "angry"), ("cancelled", None)])
async def test_lifecycle_ack_then_result_or_cancel_cleanup(adapter, outcome, expected):
    adapter._reactions_enabled = lambda: True
    adapter._connector_request = AsyncMock()
    event = SimpleNamespace(source=SimpleNamespace(chat_id="19:abc"), message_id="99")
    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, SimpleNamespace(value=outcome))
    calls = adapter._connector_request.await_args_list
    assert calls[0].args == ("PUT", "19:abc", "99") and calls[0].kwargs["reaction"] == "1f440_eyes"
    assert calls[1].args == ("DELETE", "19:abc", "99")
    if expected:
        assert calls[2].kwargs["reaction"] == expected
    else:
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_disabled_lifecycle_leaves_explicit_reaction_actions_available(adapter):
    adapter._reactions_enabled = lambda: False
    adapter._connector_request = AsyncMock()
    event = SimpleNamespace(source=SimpleNamespace(chat_id="19:abc"), message_id="99")
    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, "success")
    adapter._connector_request.assert_not_awaited()
    assert (await adapter.add_reaction("19:abc", "👍", "99"))["success"]


def reaction_activity(sender="human"):
    return SimpleNamespace(id="evt-1", reply_to_id="msg-99", from_=SimpleNamespace(id=sender, name="Human"),
                           recipient=SimpleNamespace(id="28:bot-id"),
                           conversation=SimpleNamespace(id="19:abc;messageid=42", conversation_type="channel"),
                           reactions_added=[{"type": "like"}], reactions_removed=[])


@pytest.mark.asyncio
async def test_inbound_reactions_deduplicate_and_ignore_bot_echoes(adapter):
    ctx = SimpleNamespace(activity=reaction_activity())
    await adapter._on_message_reaction(ctx)
    await adapter._on_message_reaction(ctx)
    await adapter._on_message_reaction(SimpleNamespace(activity=reaction_activity("28:bot-id")))
    adapter._reaction_handler.assert_awaited_once()
    adapter._platform_event_handler.assert_awaited_once()
    event = adapter._reaction_handler.await_args.args[0]
    assert event["reaction"] == "👍" and event["message_ts"] == "msg-99"
    assert event["channel_id"] == "19:abc;messageid=42"


@pytest.mark.asyncio
async def test_inbound_reaction_requires_target_activity(adapter):
    activity = reaction_activity()
    activity.reply_to_id = None
    await adapter._on_message_reaction(SimpleNamespace(activity=activity))
    adapter._reaction_handler.assert_not_awaited()


@pytest.mark.parametrize("value, expected", [("NaN", None), ("inf", None), ("garbage", None), ("-2", 0), ("1.5", 1.5)])
def test_retry_after_parser_rejects_nonfinite_and_invalid_values(value, expected):
    assert transport._retry_after_seconds(http_error(429, value)) == expected
