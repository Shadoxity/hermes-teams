"""Structured Teams capabilities, available to the agent and scheduled CLI work."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path


def configured():
    from gateway.config import PlatformConfig
    from hermes_cli.config import load_config_readonly
    from .adapter import validate_config
    try:
        teams = (load_config_readonly().get("platforms") or {}).get("teams") or {}
        return validate_config(PlatformConfig(extra=teams.get("extra") or {}))
    except Exception:
        return False


def standalone_adapter():
    from gateway.config import PlatformConfig
    from hermes_cli.config import load_config_readonly
    from gateway.platforms._shared import get_scoped_secret
    from .adapter import TeamsAdapter
    config = load_config_readonly()
    teams = (config.get("platforms") or {}).get("teams") or {}
    extra = dict(teams.get("extra") or {})
    if not extra.get("service_url"):
        service_url = get_scoped_secret("TEAMS_SERVICE_URL", "")
        if service_url:
            extra["service_url"] = service_url
    return TeamsAdapter(PlatformConfig(enabled=True, extra=extra))


async def close_adapter(adapter):
    if adapter is not None and adapter._graph_files is not None:
        await adapter._graph_files.aclose()


async def post_tool(args, **kwargs):
    adapter = None
    try:
        adapter = standalone_adapter()
        result = await adapter.send_rich_post(args["chat_id"], args["post"], reply_to=args.get("reply_to"))
        return json.dumps({"success": result.success, "message_id": result.message_id, "error": result.error})
    except Exception as exc:
        error = adapter._file_error(exc) if adapter is not None else "Unable to load this profile's Teams configuration"
        return json.dumps({"success": False, "error": error})
    finally:
        await close_adapter(adapter)


async def files_tool(args, **kwargs):
    adapter = None
    try:
        adapter = standalone_adapter()
        action, chat_id = args.get("action"), args["chat_id"]
        if action == "download":
            include_thread = args.get("include_thread", False)
            if not isinstance(include_thread, bool):
                return json.dumps({"success": False, "error": "include_thread must be a boolean"})
            files = await adapter.download_message_files(chat_id, args["message_id"],
                root_id=args.get("root_id"), include_thread=include_thread)
            return json.dumps({"success": True, "files": files})
        if action == "upload":
            result = await adapter.send_document(chat_id, args["path"], caption=args.get("caption"),
                file_name=args.get("filename"), reply_to=args.get("root_id"))
            return json.dumps({"success": result.success, "message_id": result.message_id, "error": result.error})
        return json.dumps({"success": False, "error": "action must be download or upload"})
    except Exception as exc:
        error = adapter._file_error(exc) if adapter is not None else "Unable to load this profile's Teams configuration"
        return json.dumps({"success": False, "error": error})
    finally:
        await close_adapter(adapter)


_POST_PROPERTIES = {
    "template": {"type": "string", "enum": ["report", "status", "document"]},
    "title": {"type": "string"}, "summary": {"type": "string"}, "status": {"type": "string"},
    "facts": {"type": "array", "items": {"type": "object", "properties": {
        "label": {"type": "string"}, "value": {"type": "string"}}, "required": ["label", "value"]}},
    "sections": {"type": "array", "items": {"type": "object", "properties": {
        "title": {"type": "string"}, "text": {"type": "string"}}, "required": ["text"]}},
    "actions": {"type": "array", "items": {"type": "object", "properties": {
        "label": {"type": "string"}, "url": {"type": "string"}}, "required": ["label", "url"]}},
}


def register_tools(ctx):
    ctx.register_tool(name="teams_post", toolset="hermes_teams", is_async=True,
        handler=post_tool, check_fn=configured,
        description="Publish a structured report, status or document card to a Teams channel.",
        schema={"name": "teams_post", "description": "Post a polished Teams Adaptive Card. Use only for an intended channel post; check the returned message_id for delivery.",
                "parameters": {"type": "object", "properties": {
                    "chat_id": {"type": "string", "description": "Teams Bot Framework conversation ID; preserve ;messageid= to reply in its thread."},
                    "reply_to": {"type": "string"},
                    "post": {"type": "object", "properties": _POST_PROPERTIES,
                             "required": ["template", "title"], "additionalProperties": False},
                }, "required": ["chat_id", "post"]}})
    ctx.register_tool(name="teams_files", toolset="hermes_teams", is_async=True,
        handler=files_tool, check_fn=configured,
        description="Download files from channel messages/threads or upload a file and post its link.",
        schema={"name": "teams_files", "description": "Download files attached to a Teams channel message or thread, or upload a generated local file into channel SharePoint storage and post a file card. Requires Graph file/message permissions. Use root_id for a channel reply.",
                "parameters": {"type": "object", "properties": {
                    "action": {"type": "string", "enum": ["download", "upload"]},
                    "chat_id": {"type": "string"}, "message_id": {"type": "string"},
                    "root_id": {"type": "string"}, "include_thread": {"type": "boolean"},
                    "path": {"type": "string"}, "filename": {"type": "string"}, "caption": {"type": "string"},
                }, "required": ["action", "chat_id"]}})
    ctx.register_cli_command("teams-post", "Post a structured Teams card from JSON", _setup_cli, _run_cli)


def _setup_cli(parser):
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--post-json", type=Path, required=True)
    parser.add_argument("--reply-to")
    parser.add_argument("--validate-only", action="store_true", help="Render and validate without contacting Teams")


def _run_cli(args):
    post = json.loads(args.post_json.read_text(encoding="utf-8"))
    if args.validate_only:
        from .cards import build_card
        print(json.dumps(build_card(post), ensure_ascii=False, indent=2))
        return
    result = asyncio.run(post_tool({"chat_id": args.chat_id, "reply_to": args.reply_to, "post": post}))
    print(result)
    if not json.loads(result).get("success"):
        raise SystemExit(1)
