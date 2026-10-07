"""Summary delivery preserves profile boundaries and never returns webhook secrets."""
from contextvars import ContextVar
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest


WEBHOOK = "https://example.test/incoming/PRIVATE-WEBHOOK?sig=PRIVATE-SIGNATURE"
PAYLOAD = SimpleNamespace(title="Meeting", summary="Decisions", key_decisions=[], action_items=[], risks=[])


@pytest.fixture
def summary_module(monkeypatch):
    """Load without the Hermes runtime; never replace the real module permanently."""
    scope = ContextVar("summary_test_scope", default={})
    config = ModuleType("gateway.config")
    config.PlatformConfig = SimpleNamespace
    i18n = ModuleType("agent.i18n")
    i18n.t = lambda key, **values: key + (" " + str(values) if values else "")
    shared = ModuleType("gateway.platforms._shared")
    shared.get_scoped_secret = lambda key, default=None: scope.get().get(key, default)
    for name, module in (("gateway.config", config), ("agent.i18n", i18n), ("gateway.platforms._shared", shared)):
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("summary_writer_under_test", Path(__file__).parents[1] / "summary_writer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.test_scope = scope
    return module


@pytest.mark.parametrize("key,env", [
    ("delivery_mode", "TEAMS_DELIVERY_MODE"), ("incoming_webhook_url", "TEAMS_INCOMING_WEBHOOK_URL"),
    ("access_token", "TEAMS_GRAPH_ACCESS_TOKEN"), ("team_id", "TEAMS_TEAM_ID"),
    ("channel_id", "TEAMS_CHANNEL_ID"), ("chat_id", "TEAMS_CHAT_ID"),
])
def test_all_delivery_settings_follow_active_profile_not_process(summary_module, monkeypatch, key, env):
    monkeypatch.setenv(env, "another-profile-value")
    writer = summary_module.TeamsSummaryWriter()
    assert key not in writer._resolve_delivery_config({})
    summary_module.test_scope.set({env: "active-profile-value"})
    assert writer._resolve_delivery_config({})[key] == "active-profile-value"


def test_explicit_and_platform_configuration_keep_precedence(summary_module):
    summary_module.test_scope.set({"TEAMS_CHAT_ID": "scope-chat", "TEAMS_GRAPH_ACCESS_TOKEN": "scope-token"})
    config = SimpleNamespace(extra={"chat_id": "platform-chat"}, token="platform-token",
                             home_channel=SimpleNamespace(chat_id="home-channel"))
    result = summary_module.TeamsSummaryWriter(config)._resolve_delivery_config({"chat_id": "explicit-chat"})
    assert result["chat_id"] == "explicit-chat"
    assert result["access_token"] == "platform-token"
    assert result["channel_id"] == "home-channel"


@pytest.mark.asyncio
async def test_concurrent_profiles_post_only_to_their_own_webhook(summary_module, monkeypatch):
    import asyncio
    monkeypatch.setenv("TEAMS_INCOMING_WEBHOOK_URL", "https://wrong.example.test/secret")
    targets = []

    async def handler(request):
        targets.append(str(request.url))
        await asyncio.sleep(0)
        return httpx.Response(200)

    writer = summary_module.TeamsSummaryWriter(transport=httpx.MockTransport(handler))

    async def deliver(url):
        token = summary_module.test_scope.set({"TEAMS_INCOMING_WEBHOOK_URL": url})
        try:
            return await writer.write_summary(PAYLOAD, {})
        finally:
            summary_module.test_scope.reset(token)

    results = await asyncio.gather(deliver(WEBHOOK), deliver("https://second.example.test/other-secret"))
    assert set(targets) == {WEBHOOK, "https://second.example.test/other-secret"}
    assert results == [{"delivery_mode": "incoming_webhook", "status_code": 200, "delivered": True}] * 2
    assert "secret" not in json.dumps(results).lower()


@pytest.mark.asyncio
async def test_legacy_record_redacts_nested_secrets_without_mutating_or_resending(summary_module):
    legacy = {"delivery_mode": "incoming_webhook", "webhook_url": WEBHOOK, "delivered": True,
              "status_code": 200, "error": "Old error for " + WEBHOOK,
              "nested": [{"incomingWebhookUrl": WEBHOOK, "access_token": "TOKEN", "note": "Old TOKEN"}]}
    original = json.dumps(legacy, sort_keys=True)
    writer = summary_module.TeamsSummaryWriter()
    writer._write_summary_via_incoming_webhook = AsyncMock()
    result = await writer.write_summary(PAYLOAD, {}, legacy)
    assert result["delivered"] and result["status_code"] == 200
    assert "PRIVATE" not in json.dumps(result) and "TOKEN" not in json.dumps(result)
    assert result["nested"] == [{"note": "Old [redacted]"}]
    assert json.dumps(legacy, sort_keys=True) == original
    writer._write_summary_via_incoming_webhook.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_record_error_redacts_current_scoped_webhook(summary_module):
    summary_module.test_scope.set({"TEAMS_INCOMING_WEBHOOK_URL": WEBHOOK})
    result = await summary_module.TeamsSummaryWriter().write_summary(PAYLOAD, {}, {"error": "POST " + WEBHOOK})
    assert result == {"error": "POST [redacted]"}


@pytest.mark.asyncio
async def test_force_resend_returns_fresh_safe_evidence(summary_module):
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(202))
    result = await summary_module.TeamsSummaryWriter(transport=transport).write_summary(
        PAYLOAD, {"incoming_webhook_url": WEBHOOK, "force_resend": True}, {"webhook_url": WEBHOOK, "status_code": 200})
    assert len(calls) == 1 and result == {"delivery_mode": "incoming_webhook", "status_code": 202, "delivered": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403, 500])
async def test_webhook_http_errors_never_include_request_url_or_response_body(summary_module, status):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text="PRIVATE response body"))
    writer = summary_module.TeamsSummaryWriter(transport=transport)
    with pytest.raises(ValueError) as error:
        await writer.write_summary(PAYLOAD, {"incoming_webhook_url": WEBHOOK})
    assert str(error.value) == f"Teams incoming webhook delivery failed (HTTP {status})."
    assert "PRIVATE" not in str(error.value) and error.value.__suppress_context__


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.InvalidURL])
async def test_webhook_transport_errors_are_redacted(summary_module, error_type):
    def handler(request):
        raise error_type("Cannot connect to " + WEBHOOK)

    writer = summary_module.TeamsSummaryWriter(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match=r"^Teams incoming webhook delivery failed\.$"):
        await writer.write_summary(PAYLOAD, {"incoming_webhook_url": WEBHOOK})


@pytest.fixture
def graph_factories(summary_module, monkeypatch):
    auth = ModuleType("tools.microsoft_graph_auth")
    clients = ModuleType("tools.microsoft_graph_client")
    seen = {}

    class Credentials:
        @classmethod
        def from_env(cls, environ=None):
            assert environ is not None, "Process-global credential lookup must not be used"
            seen["credentials"] = dict(environ)
            if not all(environ.get(key) for key in ("MSGRAPH_TENANT_ID", "MSGRAPH_CLIENT_ID", "MSGRAPH_CLIENT_SECRET")):
                raise ValueError("Missing Microsoft Graph configuration")
            return SimpleNamespace(**environ)

    auth.GraphCredentials = Credentials
    auth.MicrosoftGraphTokenProvider = lambda credentials, **kwargs: SimpleNamespace(credentials=credentials, **kwargs)
    clients.MicrosoftGraphClient = lambda provider, **kwargs: SimpleNamespace(token_provider=provider, **kwargs)
    monkeypatch.setitem(sys.modules, "tools.microsoft_graph_auth", auth)
    monkeypatch.setitem(sys.modules, "tools.microsoft_graph_client", clients)
    return seen


def test_graph_credentials_scope_and_authority_never_borrow_other_profile(summary_module, graph_factories, monkeypatch):
    scoped = {key: "scoped-" + key for key in summary_module._GRAPH_ENV_KEYS}
    for key in scoped:
        monkeypatch.setenv(key, "wrong-global-" + key)
    summary_module.test_scope.set(scoped)
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    client = summary_module.TeamsSummaryWriter(transport=transport)._build_graph_client({})
    assert graph_factories["credentials"] == scoped
    assert client.token_provider.transport is transport and client.transport is transport


def test_missing_scoped_graph_secret_does_not_mix_in_global_secret(summary_module, graph_factories, monkeypatch):
    monkeypatch.setenv("MSGRAPH_CLIENT_SECRET", "OTHER-PROFILE-SECRET")
    summary_module.test_scope.set({"MSGRAPH_TENANT_ID": "tenant", "MSGRAPH_CLIENT_ID": "app"})
    with pytest.raises(ValueError, match="Missing Microsoft Graph configuration"):
        summary_module.TeamsSummaryWriter()._build_graph_client({})
    assert graph_factories["credentials"]["MSGRAPH_CLIENT_SECRET"] == ""


@pytest.mark.asyncio
async def test_explicit_access_token_interface_still_works(summary_module, graph_factories):
    client = summary_module.TeamsSummaryWriter()._build_graph_client({"access_token": " explicit-token "})
    assert await client.token_provider.get_access_token() == "explicit-token"
    assert "credentials" not in graph_factories


@pytest.mark.asyncio
async def test_graph_delivery_retains_message_evidence_and_escaped_html(summary_module):
    graph = SimpleNamespace(post_json=AsyncMock(return_value={"id": "message", "webUrl": "https://teams.microsoft.com/message"}))
    writer = summary_module.TeamsSummaryWriter(graph_client=graph)
    result = await writer.write_summary(SimpleNamespace(title="<script>", summary="A&B"),
                                        {"delivery_mode": "graph", "team_id": "team", "channel_id": "channel:with@id"})
    args, kwargs = graph.post_json.await_args
    assert args == ("/teams/team/channels/channel%3Awith%40id/messages",)
    assert "&lt;script&gt;" in kwargs["json_body"]["body"]["content"]
    assert "A&amp;B" in kwargs["json_body"]["body"]["content"]
    assert result["message_id"] == "message" and result["target_type"] == "channel"


@pytest.mark.asyncio
async def test_graph_error_does_not_propagate_token_or_signed_url(summary_module):
    error = RuntimeError("Request failed at " + WEBHOOK + " Authorization: Bearer PRIVATE")
    error.status_code = 403
    graph = SimpleNamespace(post_json=AsyncMock(side_effect=error))
    with pytest.raises(ValueError, match=r"^Teams Graph delivery failed \(HTTP 403\)\.$"):
        await summary_module.TeamsSummaryWriter(graph_client=graph).write_summary(PAYLOAD, {"chat_id": "chat"})
