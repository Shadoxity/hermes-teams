"""Contract tests against actual Hermes classes and the installed Teams SDK.

Run with scripts/test_with_hermes.py. These tests do not stub Hermes modules,
create listeners, or make network requests. Network boundaries alone are mocked.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

if not os.environ.get("HERMES_TEST_SOURCE"):
    pytest.skip("Use scripts/test_with_hermes.py with the actual Hermes runtime", allow_module_level=True)

# When explicitly requested, unavailable core/SDK dependencies fail collection.
from gateway.config import PlatformConfig
from gateway.platforms.event import MessageEvent
from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
from microsoft_teams.api import MessageActivityInput
from hermes_teams import adapter as adapter_module
from hermes_teams import plugin_tools
from hermes_teams.adapter import TeamsAdapter
from hermes_teams.cards import build_card, render_fallback
from hermes_teams.files import ChannelTarget, extract_target
from hermes_teams.graph_files import DownloadedFile, GraphFileError

TEAM = "11111111-2222-3333-4444-555555555555"
CHANNEL = "19:channel@thread.tacv2"
THREAD = CHANNEL + ";messageid=42"
FILE_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_home_and_no_network(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n  disabled: []\n", encoding="utf-8")
    token = set_hermes_home_override(home)
    monkeypatch.setenv("TEAMS_ALLOW_ALL_USERS", "true")
    monkeypatch.delenv("TEAMS_ALLOWED_USERS", raising=False)

    def deny_connect(*args, **kwargs):
        raise AssertionError("Integration tests must not contact the network")

    monkeypatch.setattr(socket.socket, "connect", deny_connect)
    try:
        yield home
    finally:
        reset_hermes_home_override(token)


def configuration(**extra):
    return PlatformConfig(enabled=True, extra={
        "client_id": "test-bot", "client_secret": "test-secret", "tenant_id": "test-tenant",
        "stream_edit_interval": 0, **extra,
    })


@pytest.fixture
def adapter():
    value = TeamsAdapter(configuration())
    # The real gateway supplies this callback. Approval-card permissions are
    # separate and must not become an alternate authorization implementation.
    value.set_authorization_check(lambda *args, **kwargs: True)
    value._app = SimpleNamespace(
        id="test-bot", send=AsyncMock(return_value=SimpleNamespace(id="sent-1")),
        reply=AsyncMock(return_value=SimpleNamespace(id="reply-1")),
        activity_sender=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id="card-1"))))
    value.handle_message = AsyncMock()
    value._graph_files = SimpleNamespace(aclose=AsyncMock(), download_attachment=AsyncMock(return_value=DownloadedFile(
        name="report.pdf", data=FILE_BYTES, content_type="application/pdf")),
        get_message_attachments=AsyncMock(return_value=[]),
        get_thread_attachments=AsyncMock(return_value=[]),
        upload_channel_file=AsyncMock(return_value={"id": "drive-item-1", "name": "report.pdf",
            "webUrl": "https://contoso.sharepoint.com/sites/Example/Shared%20Documents/report.pdf"}))
    return value


def context(*, attachments=None, text="Please review", sender="human", message_id="99", thread=THREAD):
    activity = SimpleNamespace(
        id=message_id, text=text, from_=SimpleNamespace(id=sender, aad_object_id="human-aad", name="Human"),
        recipient=SimpleNamespace(id="28:test-bot"), reply_to_id="42", entities=[],
        conversation=SimpleNamespace(id=thread, conversation_type="channel", name="Testing", tenant_id="test-tenant"),
        channel_data={"team": {"id": "19:legacy-team@thread.tacv2", "aadGroupId": TEAM},
                      "channel": {"id": CHANNEL}},
        attachments=attachments if attachments is not None else [SimpleNamespace(
            content_type="reference", content_url="https://contoso.sharepoint.com/sites/Example/Shared%20Documents/report.pdf",
            name="report.pdf")])
    reference = SimpleNamespace(service_url="https://smba.trafficmanager.net/emea/", conversation=activity.conversation)
    return SimpleNamespace(activity=activity, conversation_ref=reference)


def bridge_request(app, payload):
    """A real aiohttp request/body stream, with no socket or listening server."""
    from aiohttp import StreamReader
    from aiohttp.test_utils import make_mocked_request

    stream = StreamReader(Mock(_reading_paused=False), 2**16, loop=asyncio.get_running_loop())
    stream.feed_data(payload)
    stream.feed_eof()
    return make_mocked_request("POST", "/api/messages", app=app, payload=stream,
                               headers={"Content-Type": "application/json", "Authorization": "Bearer secret-header"})


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 202, 401])
async def test_bridge_logs_only_activity_metadata_and_response_status(caplog, status):
    from aiohttp import web

    assert adapter_module.check_teams_requirements()
    app = web.Application()
    body = {"type": "message", "id": "12345", "text": "private-message-content",
            "attachments": [{"contentUrl": "https://example.test/private-file?secret=attachment-token"}]}
    sdk_handler = AsyncMock(return_value={"status": status, "body": {"message": "private-sdk-response"}})
    adapter_module._AiohttpBridgeAdapter(app).register_route("POST", "/api/messages", sdk_handler)
    request = bridge_request(app, json.dumps(body).encode())
    route = await app.router.resolve(request)
    with caplog.at_level(logging.INFO, logger=adapter_module.__name__):
        response = await route.handler(request)
    assert response.status == status
    assert json.loads(response.text) == {"message": "private-sdk-response"}
    assert sdk_handler.await_args.args[0] == {"body": body, "headers": dict(request.headers)}
    assert "HTTP ingress received activity_type=message activity_id=12345" in caplog.text
    assert f"HTTP ingress completed activity_type=message activity_id=12345 http_status={status}" in caplog.text
    assert all(value not in caplog.text for value in (
        "private-message-content", "private-file", "attachment-token", "secret-header", "private-sdk-response"))


@pytest.mark.asyncio
@pytest.mark.parametrize("http_error", [False, True])
async def test_bridge_logs_safe_exception_class_and_reraises(caplog, http_error):
    from aiohttp import web

    assert adapter_module.check_teams_requirements()
    app = web.Application()
    error = web.HTTPUnauthorized(reason="private-exception-secret") if http_error else RuntimeError("private-exception-secret")
    sdk_handler = AsyncMock(side_effect=error)
    adapter_module._AiohttpBridgeAdapter(app).register_route("POST", "/api/messages", sdk_handler)
    request = bridge_request(app, b'{"type":"message","id":"12345","text":"private-message-content"}')
    route = await app.router.resolve(request)
    with caplog.at_level(logging.INFO, logger=adapter_module.__name__), pytest.raises(type(error)) as raised:
        await route.handler(request)
    assert raised.value is error
    assert f"http_status={401 if http_error else 500} exception_type={type(error).__name__}" in caplog.text
    assert "private-exception-secret" not in caplog.text
    assert "private-message-content" not in caplog.text and "secret-header" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_bridge_malformed_json_logs_no_body_or_headers(caplog):
    from aiohttp import web

    app = web.Application()
    sdk_handler = AsyncMock()
    adapter_module._AiohttpBridgeAdapter(app).register_route("POST", "/api/messages", sdk_handler)
    request = bridge_request(app, b'{"text":"private-malformed-body"')
    route = await app.router.resolve(request)
    with caplog.at_level(logging.INFO, logger=adapter_module.__name__), pytest.raises(json.JSONDecodeError):
        await route.handler(request)
    sdk_handler.assert_not_awaited()
    assert "activity_type=- activity_id=- http_status=500 exception_type=JSONDecodeError" in caplog.text
    assert "private-malformed-body" not in caplog.text and "secret-header" not in caplog.text


def test_activity_log_fields_are_bounded_and_reject_log_injection():
    assert adapter_module._safe_activity_log_field("7" * 1000) == "7" * 96
    assert adapter_module._safe_activity_log_field("message", 4) == "mess"
    assert adapter_module._safe_activity_log_field("123\nprivate-injected-log") == "[invalid]"
    assert adapter_module._safe_activity_log_field({"text": "private-object-content"}) == "-"
    assert adapter_module._safe_activity_log_field(None) == "-"


@pytest.mark.asyncio
async def test_message_dispatch_and_mention_drop_logs_exclude_content(adapter, caplog):
    adapter._require_mention = True
    ctx = context(text="private-message-without-mention", message_id="safe-id")
    ctx.activity.type = "message"
    with caplog.at_level(logging.INFO, logger=adapter_module.__name__):
        await adapter._on_message(ctx)
    assert "SDK message dispatched activity_type=message activity_id=safe-id" in caplog.text
    assert "Message dropped reason=mention_required activity_id=safe-id" in caplog.text
    assert "private-message-without-mention" not in caplog.text
    assert "Human" not in caplog.text and "report.pdf" not in caplog.text
    adapter.handle_message.assert_not_awaited()


@pytest.mark.parametrize("template", ["report", "status", "document"])
def test_cards_validate_with_real_sdk_and_keep_plain_fallback(template):
    post = {"template": template, "title": "Weekly review", "summary": "Three actions are ready.",
            "facts": [{"label": "Open actions", "value": "3"}],
            "actions": [{"label": "Open report", "url": "https://contoso.sharepoint.com/report"}]}
    model = MessageActivityInput.model_validate({"type": "message", "summary": render_fallback(post),
        "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": build_card(post)}]})
    payload = model.model_dump(by_alias=True, exclude_none=True)
    assert payload["attachments"][0]["content"]["type"] == "AdaptiveCard"
    assert "Weekly review" in payload["summary"]
    assert "Weekly review" in payload["attachments"][0]["content"]["fallbackText"]
    assert not payload.get("text")


def test_installed_sdk_exposes_reaction_and_file_consent_handlers():
    with adapter_module._suppress_third_party_dotenv():
        from microsoft_teams.apps import App
    assert callable(getattr(App, "on_message_reaction", None))
    assert callable(getattr(App, "on_file_consent", None))


@pytest.mark.asyncio
async def test_inbound_channel_reference_reaches_real_media_cache_and_event(adapter, isolated_home_and_no_network):
    ctx = context()
    await adapter._on_message(ctx)
    event = adapter.handle_message.await_args.args[0]
    assert isinstance(event, MessageEvent)
    assert event.source.chat_id == THREAD and event.message_id == "99"
    assert event.media_types == ["application/pdf"] and len(event.media_urls) == 1
    path = Path(event.media_urls[0])
    assert path.read_bytes() == FILE_BYTES
    assert str(path.resolve()).startswith(str(isolated_home_and_no_network.resolve()))
    kwargs = adapter._graph_files.download_attachment.await_args.kwargs
    assert kwargs == {"team_id": TEAM, "channel_id": CHANNEL}
    assert adapter._file_target(THREAD) == ChannelTarget(TEAM, CHANNEL)
    assert "teams_files" in event.channel_prompt
    assert "skill_view" in event.channel_prompt and "hermes-teams:teams-cards" in event.channel_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [False, None, 1, "true"])
async def test_unauthorized_message_never_downloads_or_persists_file_context(adapter, authorization):
    adapter.set_authorization_check(lambda *args, **kwargs: authorization)
    await adapter._on_message(context())
    adapter._graph_files.download_attachment.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "Please review" and event.media_urls == []
    assert not adapter._file_targets and not adapter._last_inbound_by_chat


@pytest.mark.asyncio
async def test_no_gateway_authorization_callback_keeps_text_but_suppresses_downloads(adapter):
    value = TeamsAdapter(configuration())
    value._app = adapter._app
    value._graph_files = adapter._graph_files
    value.handle_message = AsyncMock()
    await value._on_message(context())
    value._graph_files.download_attachment.assert_not_awaited()
    event = value.handle_message.await_args.args[0]
    assert event.text == "Please review" and event.media_urls == []
    assert not value._file_targets and not value._last_inbound_by_chat


@pytest.mark.asyncio
async def test_mention_gate_precedes_graph_file_access(adapter):
    adapter._require_mention = True
    await adapter._on_message(context(text="Not addressed to the bot"))
    adapter._graph_files.download_attachment.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mention_shape", ["entityless-person", "entityless-bot-name", "mapping-person", "sdk-person"])
async def test_mention_gate_rejects_unverified_tags_before_authorization_or_downloads(adapter, mention_shape):
    from microsoft_teams.api import MentionEntity

    adapter._require_mention = True
    authorize = Mock(return_value=True)
    adapter.set_authorization_check(authorize)
    ctx = context(text="<at>Other person</at> Please review the attached file")
    if mention_shape == "entityless-bot-name":
        ctx.activity.text = "<at>Hermes Teams</at> Please review the attached file"
    elif mention_shape == "mapping-person":
        ctx.activity.entities = [{"type": "mention", "mentioned": {"id": "other-person"}}]
    elif mention_shape == "sdk-person":
        ctx.activity.entities = [MentionEntity.model_validate({"type": "mention", "mentioned": {"id": "other-person"}})]
    await adapter._on_message(ctx)
    authorize.assert_not_called()
    adapter._graph_files.download_attachment.assert_not_awaited()
    adapter._graph_files.get_message_attachments.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()
    assert not adapter._conv_refs and not adapter._file_targets


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["mapping", "sdk"])
@pytest.mark.parametrize("mentioned_id", ["test-bot", "28:test-bot"])
async def test_authoritative_bot_mentions_allow_attachments(adapter, shape, mentioned_id):
    from microsoft_teams.api import MentionEntity

    adapter._require_mention = True
    ctx = context(text="<at>Hermes Teams</at> Please review")
    entity = {"type": "mention", "mentioned": {"id": mentioned_id}}
    ctx.activity.entities = [MentionEntity.model_validate(entity) if shape == "sdk" else entity]
    await adapter._on_message(ctx)
    adapter._graph_files.download_attachment.assert_awaited_once()
    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.await_args.args[0].text == "Please review"


@pytest.mark.asyncio
async def test_reply_to_known_bot_message_retains_mention_exemption(adapter):
    adapter._require_mention = True
    adapter._sent_ids.append("42")
    await adapter._on_message(context(text="Please review this follow-up"))
    adapter._graph_files.download_attachment.assert_awaited_once()
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404])
async def test_inbound_missing_permissions_or_file_preserves_honest_text_event(adapter, status):
    adapter._graph_files.download_attachment.side_effect = GraphFileError(
        f"Channel file download failed (HTTP {status}); check Graph consent and site access.", status_code=status)
    await adapter._on_message(context())
    event = adapter.handle_message.await_args.args[0]
    assert event.media_urls == [] and f"HTTP {status}" in event.text
    assert "attachment unavailable" in event.text


@pytest.mark.asyncio
async def test_html_placeholder_resolves_exact_reply_route(adapter):
    ctx = context(attachments=[], text='Please inspect <attachment id="file-1"></attachment>')
    await adapter._on_message(ctx)
    adapter._graph_files.get_message_attachments.assert_awaited_once_with(TEAM, CHANNEL, "99", root_id="42")


def test_graph_target_uses_aad_group_and_rejects_botframework_team_id():
    ctx = context()
    assert extract_target(ctx.activity) == ChannelTarget(TEAM, CHANNEL)
    del ctx.activity.channel_data["team"]["aadGroupId"]
    assert extract_target(ctx.activity) is None


@pytest.mark.asyncio
async def test_real_base_splitter_respects_teams_utf8_budget(adapter):
    content = "😀" * 8000
    roots = []

    async def create_root(chat_id, payload):
        roots.append((chat_id, payload))
        return {"id": "42", "conversation_id": THREAD}

    adapter._create_channel_post = create_root
    result = await adapter.send(CHANNEL, content)
    assert result.success
    chunks = [payload["text"] for _chat, payload in roots]
    for call in adapter._app.reply.await_args_list:
        payload = call.args[2]
        chunks.append(payload if isinstance(payload, str) else payload.text)
    assert len(roots) == 1 and roots[0][0] == CHANNEL
    assert all(call.args[:2] == (CHANNEL, "42") for call in adapter._app.reply.await_args_list)
    assert len(chunks) > 1 and all(len(chunk.encode("utf-8")) <= adapter.MAX_MESSAGE_LENGTH for chunk in chunks)
    assert sum(chunk.count("😀") for chunk in chunks) == 8000
    adapter._app.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_live_app", [False, True])
async def test_new_channel_card_uses_create_conversation_even_with_live_sdk(adapter, has_live_app):
    app = adapter._app
    if not has_live_app:
        adapter._app = None
    adapter._request_activity_json = AsyncMock(return_value={"id": "42", "conversation_id": THREAD})
    result = await adapter.send_rich_post(CHANNEL, {"template": "report", "title": "Ready"})
    assert result.success and result.message_id == "42"
    url, payload = adapter._request_activity_json.await_args.args
    assert url.endswith("/v3/conversations")
    assert payload["isGroup"] is True and payload["tenantId"] == "test-tenant"
    assert payload["channelData"] == {"channel": {"id": CHANNEL}}
    assert payload["activity"]["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert adapter._request_activity_json.await_args.kwargs == {"create_conversation": True}
    app.send.assert_not_awaited()
    app.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_rich_post_routes_to_cached_thread_reference(adapter):
    ctx = context()
    adapter._conv_refs[THREAD] = ctx.conversation_ref
    result = await adapter.send_rich_post(THREAD, {"template": "status", "title": "Ready"})
    assert result.success and result.message_id == "card-1"
    sent, reference = adapter._app.activity_sender.send.await_args.args
    assert isinstance(sent, MessageActivityInput) and reference is ctx.conversation_ref
    assert sent.model_dump(by_alias=True)["replyToId"] == "42"
    adapter._app.send.assert_not_awaited()
    adapter._app.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_rich_post_explicit_reply_uses_sdk_reply(adapter):
    result = await adapter.send_rich_post(CHANNEL, {"template": "report", "title": "Ready"}, reply_to="42")
    assert result.success and result.message_id == "reply-1"
    chat, root, model = adapter._app.reply.await_args.args
    assert chat == CHANNEL and root == "42" and isinstance(model, MessageActivityInput)
    adapter._app.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_document_upload_uses_graph_and_posts_durable_file_card(adapter, tmp_path):
    path = tmp_path / "report.pdf"
    path.write_bytes(FILE_BYTES)
    adapter._file_targets[CHANNEL] = ChannelTarget(TEAM, CHANNEL)
    result = await adapter.send_document(THREAD, str(path), caption="The full report")
    assert result.success
    adapter._graph_files.upload_channel_file.assert_awaited_once_with(TEAM, CHANNEL, str(path), filename=None)
    sent = adapter._app.send.await_args.args[1]
    payload = sent.model_dump(by_alias=True, exclude_none=True)
    assert payload["replyToId"] == "42"
    assert payload["attachments"][0]["content"]["actions"][0]["url"].startswith("https://contoso.sharepoint.com/")
    assert "data:" not in json.dumps(payload)


def test_adapter_state_and_credentials_belong_to_each_profile(tmp_path):
    first_home, second_home = tmp_path / "first", tmp_path / "second"
    first_home.mkdir()
    second_home.mkdir()
    token = set_hermes_home_override(first_home)
    try:
        first = TeamsAdapter(configuration(client_id="first-bot"))
        assert first._targets_file == first_home / "plugin-data" / "hermes-teams" / "channels.json"
        first._remember_file_context(context().activity)
    finally:
        reset_hermes_home_override(token)
    token = set_hermes_home_override(second_home)
    try:
        second = TeamsAdapter(configuration(client_id="second-bot"))
        assert second._file_targets == {}
        assert second._targets_file != first._targets_file
        assert first._client_id == "first-bot" and second._client_id == "second-bot"
        assert first._graph_files is None and second._graph_files is None
    finally:
        reset_hermes_home_override(token)


def write_profile_config(home, *, client_id="yaml-bot", **extra):
    # JSON is valid YAML, so this exercises the real read-only config loader.
    home.mkdir(parents=True, exist_ok=True)
    config = {"platforms": {"teams": {"enabled": True, "extra": {
        "client_id": client_id, "client_secret": "yaml-test-secret", "tenant_id": "yaml-test-tenant",
        "file_targets": {CHANNEL: {"team_id": TEAM, "channel_id": CHANNEL}}, **extra,
    }}}}
    (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")


def test_tools_accept_yaml_only_credentials_through_actual_profile_loader(isolated_home_and_no_network, monkeypatch):
    for name in ("TEAMS_CLIENT_ID", "TEAMS_CLIENT_SECRET", "TEAMS_TENANT_ID"):
        monkeypatch.delenv(name, raising=False)
    write_profile_config(isolated_home_and_no_network)
    assert plugin_tools.configured() is True
    value = plugin_tools.standalone_adapter()
    assert (value._client_id, value._client_secret, value._tenant_id) == (
        "yaml-bot", "yaml-test-secret", "yaml-test-tenant")
    assert value._app is None and value._file_target(THREAD) == ChannelTarget(TEAM, CHANNEL)


@pytest.mark.parametrize("yaml_service", [None, "https://smba.trafficmanager.net/apac/"])
def test_standalone_tools_preserve_regional_service_url_precedence(isolated_home_and_no_network, monkeypatch, yaml_service):
    regional = "https://smba.trafficmanager.net/emea/"
    monkeypatch.setenv("TEAMS_SERVICE_URL", regional)
    write_profile_config(isolated_home_and_no_network, **({"service_url": yaml_service} if yaml_service else {}))
    value = plugin_tools.standalone_adapter()
    assert value._transport_service_url_for(CHANNEL) == (yaml_service or regional)


@pytest.mark.asyncio
async def test_post_tool_constructs_fresh_adapter_in_current_profile(tmp_path, monkeypatch):
    recorded = []

    async def network_boundary(self, chat_id, payload, reply_to=None):
        recorded.append((self._client_id, self._targets_file, chat_id, reply_to, payload))
        return {"id": self._client_id + "-sent"}

    monkeypatch.setattr(TeamsAdapter, "_post_activity", network_boundary)
    for name in ("first", "second"):
        home = tmp_path / name
        write_profile_config(home, client_id=name + "-bot")
        token = set_hermes_home_override(home)
        try:
            result = json.loads(await plugin_tools.post_tool({"chat_id": CHANNEL, "reply_to": "42",
                "post": {"template": "status", "title": "Ready"}}))
            assert result["success"] and result["message_id"] == name + "-bot-sent"
        finally:
            reset_hermes_home_override(token)
    assert [row[0] for row in recorded] == ["first-bot", "second-bot"]
    assert recorded[0][1] != recorded[1][1]
    assert all(row[2:4] == (CHANNEL, "42") for row in recorded)


@pytest.mark.asyncio
async def test_files_tool_download_dispatch_preserves_reply_and_cached_bytes(adapter, monkeypatch):
    adapter._file_targets[CHANNEL] = ChannelTarget(TEAM, CHANNEL)
    reference = {"contentType": "reference", "contentUrl": "https://contoso.sharepoint.com/report.pdf", "name": "report.pdf"}
    adapter._graph_files.get_message_attachments.return_value = [reference]
    monkeypatch.setattr(plugin_tools, "standalone_adapter", lambda: adapter)
    result = json.loads(await plugin_tools.files_tool({"action": "download", "chat_id": THREAD,
        "message_id": "99", "root_id": "42", "include_thread": False}))
    assert result["success"] and len(result["files"]) == 1
    assert Path(result["files"][0]["path"]).read_bytes() == FILE_BYTES
    adapter._graph_files.get_message_attachments.assert_awaited_once_with(TEAM, CHANNEL, "99", root_id="42")
    adapter._graph_files.get_thread_attachments.assert_not_awaited()
    adapter._graph_files.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_files_tool_upload_dispatch_preserves_filename_caption_and_thread(adapter, tmp_path, monkeypatch):
    path = tmp_path / "draft.pdf"
    path.write_bytes(FILE_BYTES)
    adapter._file_targets[CHANNEL] = ChannelTarget(TEAM, CHANNEL)
    monkeypatch.setattr(plugin_tools, "standalone_adapter", lambda: adapter)
    result = json.loads(await plugin_tools.files_tool({"action": "upload", "chat_id": CHANNEL,
        "path": str(path), "filename": "final.pdf", "caption": "Reviewed document", "root_id": "42"}))
    assert result["success"] and result["message_id"] == "reply-1"
    adapter._graph_files.upload_channel_file.assert_awaited_once_with(TEAM, CHANNEL, str(path), filename="final.pdf")
    chat, reply, model = adapter._app.reply.await_args.args
    assert (chat, reply) == (CHANNEL, "42") and "Reviewed document" in model.summary
    adapter._graph_files.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_files_tool_does_not_treat_string_false_as_permission_to_read_thread(adapter, monkeypatch):
    adapter._file_targets[CHANNEL] = ChannelTarget(TEAM, CHANNEL)
    monkeypatch.setattr(plugin_tools, "standalone_adapter", lambda: adapter)
    result = json.loads(await plugin_tools.files_tool({"action": "download", "chat_id": THREAD,
        "message_id": "99", "include_thread": "false"}))
    assert result["success"] is False
    adapter._graph_files.get_message_attachments.assert_not_awaited()
    adapter._graph_files.get_thread_attachments.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", ["post_tool", "files_tool"])
async def test_tool_config_construction_failure_is_a_safe_json_result(monkeypatch, handler_name):
    def bad_configuration():
        raise ValueError("Invalid config contains do-not-expose-test-secret")

    monkeypatch.setattr(plugin_tools, "standalone_adapter", bad_configuration)
    result = json.loads(await getattr(plugin_tools, handler_name)({"chat_id": CHANNEL}))
    assert result["success"] is False and result["error"]
    assert "do-not-expose-test-secret" not in result["error"]


def test_cli_validate_only_uses_real_parser_without_credentials_or_adapter(tmp_path, monkeypatch, capsys):
    path = tmp_path / "post.json"
    path.write_text(json.dumps({"template": "report", "title": "Weekly review"}), encoding="utf-8")
    parser = argparse.ArgumentParser()
    plugin_tools._setup_cli(parser)
    args = parser.parse_args(["--chat-id", CHANNEL, "--post-json", str(path), "--validate-only"])
    def forbidden():
        raise AssertionError("Validation must not create an adapter or read credentials")
    monkeypatch.setattr(plugin_tools, "standalone_adapter", forbidden)
    plugin_tools._run_cli(args)
    card = json.loads(capsys.readouterr().out)
    assert card["type"] == "AdaptiveCard" and "Weekly review" in json.dumps(card)


def test_cli_failed_delivery_has_nonzero_exit_and_preserves_reply_argument(tmp_path, monkeypatch, capsys):
    path = tmp_path / "post.json"
    post = {"template": "status", "title": "Ready"}
    path.write_text(json.dumps(post), encoding="utf-8")
    parser = argparse.ArgumentParser()
    plugin_tools._setup_cli(parser)
    args = parser.parse_args(["--chat-id", CHANNEL, "--post-json", str(path), "--reply-to", "42"])
    handler = AsyncMock(return_value=json.dumps({"success": False, "error": "Permission denied"}))
    monkeypatch.setattr(plugin_tools, "post_tool", handler)
    with pytest.raises(SystemExit) as failure:
        plugin_tools._run_cli(args)
    assert failure.value.code == 1
    handler.assert_awaited_once_with({"chat_id": CHANNEL, "reply_to": "42", "post": post})
    assert json.loads(capsys.readouterr().out)["success"] is False


@pytest.mark.asyncio
async def test_standalone_sender_uploads_media_and_preserves_reply(adapter, tmp_path, monkeypatch):
    path = tmp_path / "report.pdf"
    path.write_bytes(FILE_BYTES)
    adapter._app = None
    adapter._file_targets[CHANNEL] = ChannelTarget(TEAM, CHANNEL)
    posts = []
    async def post(chat_id, payload, reply_to=None):
        posts.append((chat_id, payload, reply_to))
        return {"id": "standalone-1"}
    adapter._post_activity = post
    monkeypatch.setattr(adapter_module, "TeamsAdapter", lambda config: adapter)
    # Only the outer construction and actual network boundary are replaced;
    # send_document, rich card validation, and file routing execute normally.
    result = await adapter_module._standalone_send(configuration(), CHANNEL, "Report",
        thread_id="42", media_files=[(str(path), False)], force_document=True)
    assert result["success"] and result["message_id"] == "standalone-1"
    adapter._graph_files.upload_channel_file.assert_awaited_once()
    assert posts and (posts[-1][2] == "42" or ";messageid=42" in posts[-1][0])
    assert posts[-1][1]["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"


def test_public_discovery_selects_custom_teams_once(tmp_path):
    home = tmp_path / "discovery-home"
    destination = home / "plugins" / "hermes-teams"
    destination.parent.mkdir(parents=True)
    shutil.copytree(ROOT, destination, ignore=shutil.ignore_patterns(
        ".git", "upstream", "__pycache__", ".pytest_cache", ".test-deps", ".venv", "tests"))
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [hermes-teams]\n  disabled: [platforms/teams, teams-platform]\n", encoding="utf-8")
    code = r'''
import json, os, socket
from pathlib import Path

def deny(*args, **kwargs):
    raise AssertionError("Plugin discovery attempted network access")
socket.socket.connect = deny
from hermes_cli.plugins import discover_plugins, get_plugin_manager
from hermes_cli.plugins_discovery import collect_directory_manifests, gate_manifest
from gateway.platform_registry import platform_registry
from tools.registry import registry as tool_registry
from gateway.config import PlatformConfig
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
handle = set_hermes_home_override(Path(os.environ["HERMES_HOME"]))
try:
    bundled = [manifest for manifest in collect_directory_manifests()
               if manifest.source == "bundled" and Path(manifest.path).name == "teams"
               and Path(manifest.path).parent.name == "platforms"]
    assert len(bundled) == 1, "Expected one bundled Teams manifest"
    gate = gate_manifest(bundled[0], {"platforms/teams", "teams-platform"}, {"hermes-teams"})
    assert gate.action == "placeholder" and gate.error == "disabled via config", repr(gate)
    discover_plugins(force=True)
    entry = platform_registry.get("teams")
    assert entry is not None, "Teams platform was not registered"
    assert entry.plugin_name == "hermes-teams", repr(entry.plugin_name)
    value = entry.adapter_factory(PlatformConfig(enabled=True, extra={"client_id":"test", "client_secret":"test", "tenant_id":"test"}))
    assert Path(__import__(value.__class__.__module__, fromlist=["x"]).__file__).resolve().is_relative_to(Path(os.environ["PLUGIN_EXPECTED_ROOT"]).resolve())
    assert list(platform_registry.registered_names()).count("teams") == 1
    assert hasattr(value, "download_message_files") and hasattr(value, "send_rich_post")
    for tool_name in ("teams_post", "teams_files"):
        assert tool_registry.get_entry(tool_name) is not None, tool_name
        assert tool_registry.get_toolset_for_tool(tool_name) == "hermes_teams"
        schema = tool_registry.get_schema(tool_name)
        assert schema.get("function", schema)["name"] == tool_name
    assert "teams-post" in get_plugin_manager()._cli_commands
    from tools.skills_tool import skills_list, skill_view
    skill_name = "hermes-teams:teams-cards"
    skill_root = Path(os.environ["PLUGIN_EXPECTED_ROOT"]).resolve() / "skills" / "teams-cards"
    registered_skill = get_plugin_manager().find_plugin_skill(skill_name)
    assert registered_skill is not None, "The installed plugin did not register its card skill"
    assert registered_skill.resolve() == skill_root / "SKILL.md"
    for category in (None, "plugin"):
        listing = json.loads(skills_list(category=category))
        assert listing["success"], listing
        matches = [item for item in listing["skills"] if item["name"] == skill_name]
        assert len(matches) == 1 and matches[0]["category"] == "plugin", matches
        assert matches[0]["description"].strip()
    viewed = json.loads(skill_view(name=skill_name, preprocess=False))
    assert viewed["success"] and viewed["name"] == skill_name, viewed
    assert viewed["content"].endswith(registered_skill.read_text(encoding="utf-8-sig"))
    references = {name.replace("\\", "/") for name in viewed["linked_files"]["references"]}
    for reference in ("references/layout-recipes.md", "references/tool-reference.md"):
        assert reference in references, references
        loaded = json.loads(skill_view(name=skill_name, file_path=reference))
        assert loaded["success"] and loaded["name"] == skill_name, loaded
        assert loaded["file"] == reference
        assert Path(loaded["_source_path"]).resolve() == skill_root / reference
        assert loaded["content"] == (skill_root / reference).read_text(encoding="utf-8-sig")
    print("HERMES_TEAMS_DISCOVERY_OK")
finally:
    reset_hermes_home_override(handle)
'''
    env = os.environ.copy()
    import_paths = [os.environ["HERMES_TEST_SOURCE"], str(ROOT)]
    if os.environ.get("HERMES_TEAMS_TEST_DEPS"):
        import_paths.insert(0, os.environ["HERMES_TEAMS_TEST_DEPS"])
    env.update(HERMES_HOME=str(home), HOME=str(tmp_path / "user"), USERPROFILE=str(tmp_path / "user"),
               PLUGIN_EXPECTED_ROOT=str(destination), PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join(import_paths))
    completed = subprocess.run([sys.executable, "-B", "-c", code], cwd=tmp_path, env=env,
                               text=True, capture_output=True, timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "HERMES_TEAMS_DISCOVERY_OK" in completed.stdout
