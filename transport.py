"""Teams reactions and edit-based streaming, ported from NousResearch PRs 118102/118127.

Original contributions: tournierjc / Nous Research (MIT; see LICENSE and upstream/).
Transport lives entirely in this plugin. Only the connector receives bot tokens;
update failures are returned to Hermes, never converted to an extra message here.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlsplit

logger = logging.getLogger(__name__)
_DEFAULT_SERVICE_URL = "https://smba.trafficmanager.net/teams/"
_CONNECTOR_HOSTS = frozenset({"smba.trafficmanager.net", "smba.infra.gov.teams.microsoft.us"})
_ID_RE = re.compile(r"[A-Za-z0-9:@_.-]+\Z")
_REACTION_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_CACHE_LIMIT = 128
_REACTIONS = {
    "👍": "like", "+1": "like", "thumbsup": "like", "like": "like",
    "❤️": "heart", "❤": "heart", "♥️": "heart", "heart": "heart",
    "👀": "1f440_eyes", "eyes": "1f440_eyes", "1f440_eyes": "1f440_eyes",
    "✅": "2705_whiteheavycheckmark", "white_check_mark": "2705_whiteheavycheckmark",
    "2705_whiteheavycheckmark": "2705_whiteheavycheckmark",
    "🚀": "launch", "rocket": "launch", "launch": "launch",
    "📌": "1f4cc_pushpin", "pushpin": "1f4cc_pushpin", "1f4cc_pushpin": "1f4cc_pushpin",
    "😆": "laugh", "😂": "laugh", "laugh": "laugh", "😮": "surprised",
    "surprised": "surprised", "😢": "sad", "sad": "sad", "😠": "angry",
    "😡": "angry", "angry": "angry", "❌": "angry", "x": "angry",
}
_EMOJIS = {
    "like": "👍", "heart": "❤️", "1f440_eyes": "👀",
    "2705_whiteheavycheckmark": "✅", "launch": "🚀", "1f4cc_pushpin": "📌",
    "laugh": "😆", "surprised": "😮", "sad": "😢", "angry": "😠",
}


def _flat_conversation_id(chat_id: str) -> str:
    raw = str(chat_id or "").strip()
    marker = raw.lower().find(";messageid=")
    return raw[:marker] if marker >= 0 else raw


# Shared plugin-only address helper used by file delivery as well as transport.
flat_conversation_id = _flat_conversation_id


def _to_teams_reaction_type(emoji: str | None) -> str | None:
    raw = (emoji or "").strip().strip(":")
    return _REACTIONS.get(raw) or _REACTIONS.get(raw.lower()) or (raw if _REACTION_RE.fullmatch(raw) else None)


def _safe_service_url(raw: str) -> str:
    parsed = urlsplit(raw)
    if (parsed.scheme != "https" or parsed.hostname not in _CONNECTOR_HOSTS
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("Teams service URL is not an allowed HTTPS connector")
    return raw.rstrip("/") + "/"


def _bf_activity_url(service_url: str, conversation_id: str, activity_id: str) -> str:
    conv_id = _flat_conversation_id(conversation_id)
    if not _ID_RE.fullmatch(conv_id) or not _ID_RE.fullmatch(str(activity_id)):
        raise ValueError("Invalid Teams conversation or activity id")
    return (f"{_safe_service_url(service_url)}v3/conversations/{quote(conv_id, safe=':@-_.')}"
            f"/activities/{quote(str(activity_id), safe=':@-_.')}")


def _http_status_from_exc(exc: BaseException) -> int | None:
    for obj in (exc, getattr(exc, "response", None)):
        for attr in ("status_code", "status"):
            value = getattr(obj, attr, None)
            if isinstance(value, int):
                return value
    return None


def _retry_after_seconds(exc: BaseException) -> float | None:
    headers = getattr(exc, "headers", None) or getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except (ValueError, TypeError):
        try:
            when = parsedate_to_datetime(str(raw))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


def _send_result(**kwargs):
    from gateway.platforms.base import SendResult
    return SendResult(**kwargs)


def _remember(cache: dict, key: Any, value: Any) -> None:
    if key not in cache and len(cache) >= _CACHE_LIMIT:
        cache.pop(next(iter(cache)))
    cache[key] = value


@dataclass
class _EditState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_text: str | None = None
    last_sent: float = -math.inf
    pending_text: str | None = None
    final_requested: bool = False
    last_result: Any = None


class TeamsTransportMixin:
    """Use before BasePlatformAdapter in the plugin's adapter inheritance list."""

    draft_stream_is_message = False
    _ACK_EMOJI = "👀"
    _OK_EMOJI = "✅"
    _FAIL_EMOJI = "❌"

    def _init_teams_transport(self) -> None:
        self._edit_states: dict[tuple[str, str], _EditState] = {}
        self._bot_reactions: dict[tuple[str, str], str] = {}
        self._last_inbound_by_chat: dict[str, str] = {}
        self._reaction_events_seen: dict[tuple, bool] = {}

    def _transport_service_url_for(self, chat_id: str) -> str:
        # Keep the full thread key to select its reference before flattening the URL.
        refs = self._conv_refs
        ref = refs.get(str(chat_id)) or refs.get(_flat_conversation_id(chat_id))
        raw = (ref.get("serviceUrl") or ref.get("service_url")) if isinstance(ref, dict) else getattr(ref, "service_url", None)
        return _safe_service_url(str(raw or self._extra.get("service_url") or _DEFAULT_SERVICE_URL))

    async def _connector_request(self, method: str, chat_id: str, activity_id: str,
                                 *, payload: dict | None = None, reaction: str | None = None) -> None:
        import httpx
        # Validate everything before acquiring a bearer token. Never follow redirects.
        url = _bf_activity_url(self._transport_service_url_for(chat_id), chat_id, activity_id)
        if reaction is not None:
            if not _REACTION_RE.fullmatch(reaction):
                raise ValueError("Invalid Teams reaction type")
            url += "/reactions/" + quote(reaction, safe="")
        from gateway.platforms.base import gateway_trust_env
        token = await self._get_botframework_token()
        async with httpx.AsyncClient(timeout=15.0, trust_env=gateway_trust_env(), follow_redirects=False) as client:
            response = await client.request(method, url, json=payload, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()

    async def _update_activity(self, chat_id: str, activity_id: str, text: str) -> None:
        await self._connector_request("PUT", chat_id, activity_id, payload={
            "type": "message", "id": activity_id, "text": text, "textFormat": "markdown",
        })

    async def _delete_activity_via_rest(self, chat_id: str, activity_id: str) -> None:
        await self._connector_request("DELETE", chat_id, activity_id)

    def _edit_interval(self) -> float:
        try:
            value = float(self._extra.get("stream_edit_interval", 1.0))
            return min(5.0, max(0.0, value)) if math.isfinite(value) else 1.0
        except (ValueError, TypeError):
            return 1.0

    async def edit_message(self, chat_id: str, message_id: str, content: str, *,
                           finalize: bool = False, metadata: dict | None = None):
        if not self._app:
            return _send_result(success=False, error="Teams app not initialized")
        try:
            _bf_activity_url(self._transport_service_url_for(chat_id), chat_id, message_id)
        except ValueError as exc:
            return _send_result(success=False, error=str(exc))
        text = self.format_message(content)
        budget = self.MAX_MESSAGE_LENGTH
        encoded = text.encode("utf-8")
        if len(encoded) > budget:
            if finalize:
                # Hermes must split the full response through send(); never lose its tail.
                return _send_result(success=False, error="Final Teams message exceeds the activity byte limit")
            text = encoded[:budget].decode("utf-8", errors="ignore")
        key = (_flat_conversation_id(chat_id), str(message_id))
        state = self._edit_states.get(key)
        if state is None:
            if len(self._edit_states) >= _CACHE_LIMIT:
                # Do not evict an in-flight lock and accidentally create a parallel writer.
                victim = next((k for k, s in self._edit_states.items() if not s.lock.locked()), None)
                if victim is None:
                    return _send_result(success=False, error="Too many active Teams message updates", retryable=True)
                del self._edit_states[victim]
            state = self._edit_states[key] = _EditState()
        if state.final_requested and not finalize:
            return state.last_result or _send_result(success=True, message_id=str(message_id))
        state.pending_text = text
        state.final_requested |= finalize
        async with state.lock:
            if state.pending_text is None:
                return state.last_result or _send_result(success=True, message_id=str(message_id))
            wait = self._edit_interval() - (time.monotonic() - state.last_sent)
            if wait > 0:
                await asyncio.sleep(wait)
            # Concurrent callers replace pending_text while the throttle waits.
            text = state.pending_text
            state.pending_text = None
            if text == state.last_text:
                state.last_result = _send_result(success=True, message_id=str(message_id))
                return state.last_result
            try:
                await self._update_activity(str(chat_id), str(message_id), text)
            except Exception as exc:
                status = _http_status_from_exc(exc)
                retry_after = _retry_after_seconds(exc)
                if status == 429 and (retry_after is None or retry_after <= 2.0):
                    await asyncio.sleep(1.0 if retry_after is None else retry_after)
                    try:
                        await self._update_activity(str(chat_id), str(message_id), text)
                    except Exception as retry_exc:
                        state.last_result = self._edit_failure(retry_exc)
                        return state.last_result
                else:
                    state.last_result = self._edit_failure(exc)
                    return state.last_result
            state.last_sent = time.monotonic()
            state.last_text = text
            state.last_result = _send_result(success=True, message_id=str(message_id))
            return state.last_result

    @staticmethod
    def _edit_failure(exc: BaseException):
        status = _http_status_from_exc(exc)
        # Return stable errors, avoiding SDK response bodies or URLs in user-facing errors.
        error = f"Teams activity update failed (HTTP {status})" if status else "Teams activity update failed"
        return _send_result(success=False, error=error, retryable=status is None or status == 429 or status >= 500,
                            error_kind="rate_limited" if status == 429 else "not_found" if status == 404 else None,
                            retry_after=_retry_after_seconds(exc) if status == 429 else None)

    async def delete_message(self, chat_id: str, message_id: str, metadata: dict | None = None) -> bool:
        if not self._app:
            return False
        try:
            await self._delete_activity_via_rest(chat_id, message_id)
        except Exception as exc:
            if _http_status_from_exc(exc) != 404:
                return False
        self._edit_states.pop((_flat_conversation_id(chat_id), str(message_id)), None)
        return True

    def _remember_inbound_activity(self, activity: Any) -> None:
        chat_id = getattr(getattr(activity, "conversation", None), "id", None)
        message_id = getattr(activity, "id", None)
        if chat_id and message_id:
            _remember(self._last_inbound_by_chat, str(chat_id), str(message_id))

    def _reactions_enabled(self) -> bool:
        from gateway.platforms._shared import extra_or_secret
        value = extra_or_secret(self._extra, "reactions", "TEAMS_REACTIONS", True)
        return value if isinstance(value, bool) else str(value).strip().lower() not in {"false", "0", "no", "off"}

    async def _react(self, chat_id: str, message_id: str, reaction_type: str, *, remove: bool) -> bool:
        if not self._app:
            return False
        try:
            await self._connector_request("DELETE" if remove else "PUT", chat_id, message_id, reaction=reaction_type)
        except Exception as exc:
            logger.debug("Teams reaction request failed (HTTP %s)", _http_status_from_exc(exc))
            return False
        key = (_flat_conversation_id(chat_id), str(message_id))
        if remove:
            if self._bot_reactions.get(key) == reaction_type:
                self._bot_reactions.pop(key, None)
        else:
            _remember(self._bot_reactions, key, reaction_type)
        return True

    async def _add_reaction(self, chat_id: str, message_id: str, emoji: str) -> bool:
        reaction = _to_teams_reaction_type(emoji)
        return bool(reaction) and await self._react(chat_id, message_id, reaction, remove=False)

    async def _remove_reaction(self, chat_id: str, message_id: str, emoji: str | None = None) -> bool:
        reaction = _to_teams_reaction_type(emoji) if emoji else self._bot_reactions.get(
            (_flat_conversation_id(chat_id), str(message_id)), _to_teams_reaction_type(self._ACK_EMOJI))
        return bool(reaction) and await self._react(chat_id, message_id, reaction, remove=True)

    async def add_reaction(self, chat_id: str, emoji: str, message_id: str | None = None) -> dict:
        target = message_id or self._last_inbound_by_chat.get(str(chat_id))
        if not target:
            return {"success": False, "error": "No recent message in this thread; specify message_id"}
        reaction = _to_teams_reaction_type(emoji)
        if not reaction:
            return {"success": False, "error": "Unsupported Teams reaction"}
        success = await self._react(chat_id, target, reaction, remove=False)
        return {"success": True, "message_id": target, "reaction": reaction} if success else {"success": False, "error": "Teams reaction failed"}

    async def remove_reaction(self, chat_id: str, message_id: str | None = None, emoji: str | None = None) -> dict:
        target = message_id or self._last_inbound_by_chat.get(str(chat_id))
        if not target:
            return {"success": False, "error": "No recent message in this thread; specify message_id"}
        success = await self._remove_reaction(chat_id, target, emoji)
        return {"success": True, "message_id": target} if success else {"success": False, "error": "Teams reaction removal failed"}

    async def on_processing_start(self, event: Any) -> None:
        if self._reactions_enabled() and event.source.chat_id and event.message_id:
            await self._add_reaction(str(event.source.chat_id), str(event.message_id), self._ACK_EMOJI)

    async def on_processing_complete(self, event: Any, outcome: Any) -> None:
        if not self._reactions_enabled() or not event.source.chat_id or not event.message_id:
            return
        chat_id, message_id = str(event.source.chat_id), str(event.message_id)
        await self._remove_reaction(chat_id, message_id, self._ACK_EMOJI)
        outcome_name = str(getattr(outcome, "value", outcome)).lower()
        if outcome_name in {"success", "failure"}:
            await self._add_reaction(chat_id, message_id, self._OK_EMOJI if outcome_name == "success" else self._FAIL_EMOJI)

    async def _on_message_reaction(self, ctx: Any) -> None:
        activity = ctx.activity
        sender = getattr(activity, "from_", None)
        recipient_id = getattr(getattr(activity, "recipient", None), "id", None)
        bot_ids = {str(v) for v in (getattr(self._app, "id", None), recipient_id) if v}
        bot_ids |= {f"28:{v}" for v in tuple(bot_ids) if not v.startswith("28:")}
        user_id = getattr(sender, "aad_object_id", None) or getattr(sender, "id", "")
        if str(user_id) in bot_ids or getattr(sender, "id", None) in bot_ids:
            return
        conv = getattr(activity, "conversation", None)
        chat_id = getattr(conv, "id", None)
        message_id = getattr(activity, "reply_to_id", None)
        if not chat_id or not message_id or not user_id:
            return
        source = self.build_source(
            chat_id=str(chat_id), chat_name=getattr(conv, "name", None) or "",
            chat_type={"channel": "channel", "groupChat": "group"}.get(getattr(conv, "conversation_type", None), "dm"),
            user_id=str(user_id), user_name=getattr(sender, "name", None) or "",
            guild_id=getattr(conv, "tenant_id", None) or self._tenant_id, message_id=str(message_id))
        for field_name, event_name in (("reactions_added", "reaction:added"), ("reactions_removed", "reaction:removed")):
            for reaction in getattr(activity, field_name, None) or []:
                rtype = reaction.get("type") if isinstance(reaction, dict) else getattr(reaction, "type", None)
                if not rtype:
                    continue
                event_id = getattr(activity, "id", None)
                key = (str(chat_id), str(message_id), str(user_id), event_id, event_name, str(rtype))
                if event_id and key in self._reaction_events_seen:
                    continue
                if event_id:
                    _remember(self._reaction_events_seen, key, True)
                await self._emit_reaction_event(event_name, _EMOJIS.get(str(rtype), str(rtype)), str(rtype), source, activity)

    async def _emit_reaction_event(self, event_name: str, emoji: str, reaction_type: str,
                                   source: Any, raw_activity: Any) -> None:
        handler = getattr(self, "_reaction_handler", None)
        if handler is not None:
            try:
                await handler({"platform": "teams", "event_name": event_name, "reaction": emoji,
                               "user_id": source.user_id, "item_user_id": None, "channel_id": source.chat_id,
                               "message_ts": source.message_id, "event_ts": getattr(raw_activity, "id", None),
                               "raw_event": raw_activity, "reaction_type": reaction_type})
            except Exception:
                logger.debug("Teams reaction hook failed", exc_info=True)
        handler = getattr(self, "_platform_event_handler", None)
        if handler is not None:
            try:
                await handler({"platform": "teams", "event_type": "reaction", "payload": {
                    "emojis": [emoji], "chat_id": source.chat_id, "message_id": str(source.message_id),
                    "thread_id": getattr(source, "thread_id", None), "event_name": event_name,
                    "reaction_type": reaction_type}}, source)
            except Exception:
                logger.debug("Teams platform reaction hook failed", exc_info=True)
