"""Receipt-checked Bot Framework posting, including new Teams channel threads.

Channel roots use CreateConversation with an initial activity. The installed
Teams SDK's App.send targets an existing conversation and does not create a
root thread from a flat channel ID. Microsoft protocol reference:
https://github.com/microsoft/teams.net/blob/main/docs/CreateConversation-API-Behavior.md
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import httpx

from .transport import _safe_service_url, flat_conversation_id

_ACTIVITY_ID = re.compile(r"[A-Za-z0-9:@_.-]+\Z")
_THREAD = re.compile(r";messageid=([^;]+)$", re.IGNORECASE)


class PostingError(ValueError):
    """User-safe delivery error, excluding credentials and response bodies."""


def _field(value: Any, *names: str):
    for name in names:
        found = value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
        if found is not None:
            return found
    return None


def _valid_activity_id(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= 2048 and _ACTIVITY_ID.fullmatch(value) is not None


def _trust_env() -> bool:
    try:
        from gateway.platforms.base import gateway_trust_env
    except ImportError:
        return False
    return gateway_trust_env()


class TeamsPostingMixin:
    def _is_new_channel_post(self, chat_id: str, reply_to: str | None = None) -> bool:
        """Only a channel destination without an existing thread creates a root."""
        raw = str(chat_id or "")
        if reply_to or _THREAD.search(raw):
            return False
        base = flat_conversation_id(raw)
        refs = getattr(self, "_conv_refs", {})
        ref = refs.get(raw) or refs.get(base)
        kind = _field(_field(ref, "conversation"), "conversation_type", "conversationType")
        if kind in ("personal", "groupChat"):
            return False
        return (kind == "channel" or base in getattr(self, "_file_targets", {})
                or base.endswith("@thread.tacv2"))

    async def _request_activity_json(self, url: str, payload: dict, *, create_conversation: bool = False) -> dict:
        """Perform one POST; ambiguous outcomes are never retried or called delivered."""
        try:
            _safe_service_url(url)  # Validate host/scheme/port/userinfo before minting a token.
        except (TypeError, ValueError):
            raise PostingError("Teams delivery requires an allowed HTTPS connector URL.") from None
        try:
            token = await self._get_botframework_token()
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=False, trust_env=_trust_env()) as client:
                response = await client.post(url, json=payload, headers={"Authorization": "Bearer " + token})
        except httpx.RequestError:
            raise PostingError("Teams delivery could not be confirmed after a connection error. Check the channel before retrying.") from None
        if response.status_code not in (200, 201):
            if 200 <= response.status_code < 300:
                raise PostingError("Teams accepted the request without a confirmed delivery receipt. Check the channel before retrying.")
            raise PostingError(f"Teams message delivery failed (HTTP {response.status_code}).")
        try:
            receipt = response.json()
        except (ValueError, UnicodeError):
            raise PostingError("Teams returned no readable delivery receipt. Delivery is unconfirmed; check the channel before retrying.") from None
        if not isinstance(receipt, dict):
            raise PostingError("Teams returned an invalid delivery receipt. Check the channel before retrying.")
        if create_conversation:
            activity_id, conversation_id = receipt.get("activityId"), receipt.get("id")
            match = _THREAD.search(conversation_id) if isinstance(conversation_id, str) else None
            if not _valid_activity_id(activity_id) or not match or match.group(1) != activity_id:
                raise PostingError("Teams returned no valid channel-thread delivery receipt. Check the channel before retrying.")
            expected_channel = _field(_field(payload, "channelData"), "channel")
            expected_channel_id = _field(expected_channel, "id")
            if flat_conversation_id(conversation_id) != expected_channel_id:
                raise PostingError("Teams returned a delivery receipt for a different channel. Check the destination before retrying.")
            return {"id": activity_id, "conversation_id": conversation_id}
        if not _valid_activity_id(receipt.get("id")):
            raise PostingError("Teams returned no message ID. Delivery is unconfirmed; check the channel before retrying.")
        return {"id": receipt["id"]}

    async def _create_channel_post(self, chat_id: str, payload: dict) -> dict:
        """Create one root thread with its message and retain its returned message ID."""
        base = flat_conversation_id(str(chat_id or ""))
        if not _valid_activity_id(base) or _THREAD.search(str(chat_id)) or payload.get("replyToId"):
            raise PostingError("A new Teams channel post requires a channel ID without a thread or reply ID.")
        tenant_id = getattr(self, "_tenant_id", "")
        if not _valid_activity_id(tenant_id):
            raise PostingError("Teams channel delivery requires this profile's tenant ID.")
        service = self._transport_service_url_for(chat_id)
        request = {"isGroup": True, "tenantId": tenant_id,
                   "channelData": {"channel": {"id": base}}, "activity": payload}
        result = await self._request_activity_json(service + "v3/conversations", request, create_conversation=True)
        self._remember_sent(result)
        return result
