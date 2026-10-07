"""Recipient-bound, bounded personal-chat FileConsent uploads.

Based on the Teams consent flow in NousResearch PR 118127 (MIT). Pending
bytes never appear in invoke context, and a click cannot consume another
conversation's or recipient's file. No SDK is imported until a card is sent.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

logger = logging.getLogger(__name__)
_MIB = 1024 * 1024
_CONSENT_TYPE = "application/vnd.microsoft.teams.card.file.consent"
_INFO_TYPE = "application/vnd.microsoft.teams.card.file.info"
_MS_FILE_DOMAINS = ("sharepoint.com", "sharepoint-df.com", "onedrive.com", "1drv.com")
_SECRET_QUERY_KEYS = {"token", "accesstoken", "refreshtoken", "idtoken", "authkey",
                      "authorization", "apikey", "clientsecret", "secret", "password",
                      "sig", "signature", "code"}


def _send_result(**kwargs):
    from gateway.platforms.base import SendResult
    return SendResult(**kwargs)


def _field(value: Any, *names: str) -> Any:
    for name in names:
        found = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
        if found is not None and not type(found).__module__.startswith("unittest.mock"):
            if not isinstance(found, str) or found.strip():
                return found
    return None


def _string(value: Any, limit: int = 1024) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        return None
    return value


def _identity(account: Any) -> dict[str, str]:
    result = {}
    for label, fields in (("id", ("id",)), ("aad", ("aad_object_id", "aadObjectId"))):
        identifier = _string(_field(account, *fields))
        if identifier:
            result[label] = identifier
    return result


def _safe_name(value: Any) -> str | None:
    name = _string(value, 255)
    if not name or name in {".", ".."} or any(char in name for char in '/\\<>:"|?*'):
        return None
    return name


def _microsoft_file_url(value: Any, *, signed: bool) -> str | None:
    url = _string(value, 16384 if signed else 4096)
    if not url or "\\" in url or any(char.isspace() for char in url):
        return None
    if any(ord(char) < 32 for char in unquote(url)):
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if (parts.scheme != "https" or parts.port not in (None, 443)
                or parts.username is not None or parts.password is not None or parts.fragment
                or not any(host == domain or host.endswith("." + domain) for domain in _MS_FILE_DOMAINS)
                or not re.fullmatch(r"[A-Za-z0-9.-]+", host)):
            return None
    except ValueError:
        return None
    if not signed:
        for key, _ in parse_qsl(parts.query, keep_blank_values=True):
            key = re.sub(r"[^a-z0-9]", "", key.lower())
            if key in _SECRET_QUERY_KEYS or key.endswith("token"):
                return None
    return url


def _positive_limit(extra: dict, name: str, default: int, maximum: int) -> int:
    value = extra.get(name, default)
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(maximum, max(1, number))


@dataclass
class _PendingFile:
    chat_id: str
    recipient: dict[str, str]
    name: str
    data: bytes
    expires_at: float
    card_id: str | None = None
    timer: Any = None


class PersonalFilesMixin:
    """Initialize after ``_extra`` and ``_conv_refs``; register on_file_consent.

    Instance options: max_file_bytes (20 MiB default; 50 MiB single-PUT cap),
    personal_file_consent_ttl_seconds (900 default; 1800 cap),
    personal_pending_files (8 default; 32 cap), and personal_pending_bytes
    (40 MiB default; 80 MiB cap). Expiry timers release cached bytes even idle.
    """

    def _init_personal_files(self) -> None:
        self._personal_pending: dict[str, _PendingFile] = {}
        self._personal_pending_bytes = 0
        self._personal_inflight_count = 0
        self._personal_inflight_bytes = 0

    def _personal_limits(self) -> tuple[int, int, int, int]:
        extra = self._extra
        return (
            _positive_limit(extra, "max_file_bytes", 20 * _MIB, 50 * _MIB),
            _positive_limit(extra, "personal_file_consent_ttl_seconds", 900, 1800),
            _positive_limit(extra, "personal_pending_files", 8, 32),
            _positive_limit(extra, "personal_pending_bytes", 40 * _MIB, 80 * _MIB),
        )

    def _drop_personal_pending(self, file_id: str) -> _PendingFile | None:
        pending = self._personal_pending.pop(file_id, None)
        if pending:
            self._personal_pending_bytes -= len(pending.data)
            if pending.timer:
                pending.timer.cancel()
        return pending

    def _prune_personal_pending(self) -> None:
        now = time.monotonic()
        for file_id, pending in list(self._personal_pending.items()):
            if pending.expires_at <= now:
                self._drop_personal_pending(file_id)

    async def _send_file_consent(self, chat_id: str, file_path: str,
                                 caption: str | None = None, file_name: str | None = None):
        if not self._app:
            return _send_result(success=False, error="Teams app not initialized")
        reference = self._conv_refs.get(chat_id)
        conversation = _field(reference, "conversation")
        kind = _field(conversation, "conversation_type", "conversationType")
        kind = getattr(kind, "value", kind)
        recipient = _identity(_field(reference, "user"))
        if kind != "personal" or _field(conversation, "id") != chat_id or not recipient:
            return _send_result(success=False, error="A known personal chat and recipient are required for file consent")
        if caption is not None and (not isinstance(caption, str) or len(caption) > 2000):
            return _send_result(success=False, error="File caption must be at most 2000 characters")
        if not isinstance(file_path, str):
            return _send_result(success=False, error="A local file path is required")
        path = Path(file_path.removeprefix("file://"))
        name = _safe_name(file_name if file_name is not None else path.name)
        if not name:
            return _send_result(success=False, error="A valid file name is required")
        self._prune_personal_pending()
        maximum, ttl, max_count, max_total = self._personal_limits()
        occupied_bytes = self._personal_pending_bytes + self._personal_inflight_bytes
        if len(self._personal_pending) + self._personal_inflight_count >= max_count:
            return _send_result(success=False, error="Too many files are awaiting consent; accept or decline an existing offer")
        try:
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                return _send_result(success=False, error="Only regular files can be uploaded")
            if metadata.st_size == 0:
                return _send_result(success=False, error="Empty files are not supported by personal file consent")
            if metadata.st_size > maximum or occupied_bytes + metadata.st_size > max_total:
                return _send_result(success=False, error="File exceeds the configured personal upload or pending-byte limit")
            with path.open("rb") as stream:
                # A file may grow after stat. Never use an unbounded read.
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return _send_result(success=False, error="Only regular files can be uploaded")
                data = stream.read(min(maximum, max_total - occupied_bytes) + 1)
            if not data or len(data) > maximum or occupied_bytes + len(data) > max_total:
                return _send_result(success=False, error="File size changed or exceeds the configured upload limit")
        except (OSError, ValueError):
            return _send_result(success=False, error="Cannot read the local file")

        file_id = uuid.uuid4().hex
        pending = _PendingFile(chat_id, recipient, name, data, time.monotonic() + ttl)
        self._personal_pending[file_id] = pending
        self._personal_pending_bytes += len(data)
        pending.timer = asyncio.get_running_loop().call_later(ttl, self._drop_personal_pending, file_id)
        try:
            from microsoft_teams.api import Attachment, MessageActivityInput
            activity = MessageActivityInput().add_attachments(Attachment(
                content_type=_CONSENT_TYPE, name=name, content={
                    "description": caption or name, "sizeInBytes": len(data),
                    "acceptContext": {"file_id": file_id}, "declineContext": {"file_id": file_id},
                },
            ))
            result = await self._send_via_conv_ref(chat_id, activity, activity)
            pending.card_id = _string(_field(result, "id"))
            if self._personal_pending.get(file_id) is not pending:
                await self._dismiss_consent_card(chat_id, pending.card_id)
                return _send_result(success=False, error="The file offer expired while Teams was sending it")
            if not pending.card_id:
                self._drop_personal_pending(file_id)
                return _send_result(success=False, error="Teams did not return an ID for the file consent card")
            return _send_result(success=True, message_id=pending.card_id)
        except Exception:
            self._drop_personal_pending(file_id)
            logger.warning("[teams] Could not send personal file consent card")
            return _send_result(success=False, error="Could not send the file consent card", retryable=True)

    async def _on_file_consent(self, ctx) -> None:
        activity = _field(ctx, "activity")
        actor = _field(activity, "from_", "from")
        if self._card_action_denied(actor):
            return
        value = _field(activity, "value")
        action = _field(value, "action")
        action = str(getattr(action, "value", action) or "").lower().removeprefix("action.")
        if action not in {"accept", "decline"}:
            return
        file_id = _string(_field(_field(value, "context"), "file_id", "fileId"))
        if not file_id:
            return
        pending = self._personal_pending.get(file_id)
        if not pending:
            return  # Unknown/replayed invokes cannot dismiss an arbitrary message.
        chat_id = _field(_field(activity, "conversation"), "id")
        identity = _identity(actor)
        card_id = _field(activity, "reply_to_id", "replyToId")
        if (chat_id != pending.chat_id or not identity
                or any(identity.get(key) != identifier for key, identifier in pending.recipient.items())
                or not pending.card_id or card_id != pending.card_id):
            return  # Crucially do not pop or dismiss another recipient's offer.
        if pending.expires_at <= time.monotonic():
            self._drop_personal_pending(file_id)
            await self._personal_notice(chat_id, "That file offer has expired. Please ask for the file again.")
            await self._dismiss_consent_card(chat_id, pending.card_id)
            return
        if action == "decline":
            self._drop_personal_pending(file_id)
            await self._personal_notice(chat_id, "File upload declined.")
            await self._dismiss_consent_card(chat_id, pending.card_id)
            return

        info = _field(value, "upload_info", "uploadInfo")
        upload_url = _microsoft_file_url(_field(info, "upload_url", "uploadUrl"), signed=True)
        content_url = _microsoft_file_url(_field(info, "content_url", "contentUrl"), signed=False)
        unique_id = _string(_field(info, "unique_id", "uniqueId"), 512)
        file_type = _string(_field(info, "file_type", "fileType"), 20)
        name = _safe_name(_field(info, "name") or pending.name)
        if (not upload_url or not content_url or not unique_id or not name or not file_type
                or not re.fullmatch(r"\.?[A-Za-z0-9]+", file_type)):
            logger.warning("[teams] Personal consent has invalid file metadata or URLs")
            return
        from tools.url_safety import is_safe_url
        if not is_safe_url(upload_url):
            logger.warning("[teams] Personal upload URL failed destination validation")
            return
        # No await occurs between checking ownership and consuming the grant.
        # A concurrent/repeated invoke therefore cannot upload these bytes twice.
        self._drop_personal_pending(file_id)
        self._personal_inflight_count += 1
        self._personal_inflight_bytes += len(pending.data)
        try:
            await self._upload_consented_file(upload_url, pending.data)
        except Exception:
            logger.warning("[teams] Personal file upload failed")
            await self._personal_notice(chat_id, "File upload failed. Please ask for a new file offer.")
        else:
            try:
                await self._send_file_info_card(chat_id, {
                    "contentUrl": content_url, "uniqueId": unique_id,
                    "fileType": file_type.lstrip("."), "name": name,
                }, pending.name)
            except Exception:
                logger.warning("[teams] Personal file uploaded but its file card could not be sent")
                await self._personal_notice(chat_id, "The file uploaded, but Teams could not display its file card.")
        finally:
            self._personal_inflight_count -= 1
            self._personal_inflight_bytes -= len(pending.data)
            await self._dismiss_consent_card(chat_id, pending.card_id)

    async def _personal_notice(self, chat_id: str, text: str) -> None:
        with suppress(Exception):
            await self.send(chat_id, text)

    async def _dismiss_consent_card(self, chat_id: str, card_id: str | None) -> None:
        if card_id:
            with suppress(Exception):
                await self.delete_message(chat_id, card_id)

    async def _upload_consented_file(self, upload_url: str, data: bytes) -> None:
        from tools.url_safety import create_ssrf_safe_async_client, is_safe_url
        if (not _microsoft_file_url(upload_url, signed=True) or not is_safe_url(upload_url)
                or not isinstance(data, bytes) or not 0 < len(data) <= self._personal_limits()[0]):
            raise ValueError("Invalid personal file upload")
        size = len(data)
        headers = {"Content-Type": "application/octet-stream", "Content-Length": str(size),
                   "Content-Range": f"bytes 0-{size - 1}/{size}"}
        # The short-lived upload URL authorizes this PUT. Do not attach a bot or
        # Graph bearer, follow redirects, or buffer an unbounded response body.
        async with create_ssrf_safe_async_client(timeout=60.0, follow_redirects=False) as client:
            async with client.stream("PUT", upload_url, content=data, headers=headers) as response:
                if response.status_code not in (200, 201):
                    raise ValueError("Microsoft did not confirm completion of the personal file upload")

    async def _send_file_info_card(self, chat_id: str, upload_info: Any, fallback_name: str) -> None:
        from microsoft_teams.api import Attachment, MessageActivityInput
        name = _safe_name(_field(upload_info, "name") or fallback_name)
        content_url = _microsoft_file_url(_field(upload_info, "content_url", "contentUrl"), signed=False)
        if not name or not content_url:
            raise ValueError("Invalid personal file confirmation")
        activity = MessageActivityInput().add_attachments(Attachment(
            content_type=_INFO_TYPE, name=name, content_url=content_url,
            content={"uniqueId": _field(upload_info, "unique_id", "uniqueId"),
                     "fileType": _field(upload_info, "file_type", "fileType")},
        ))
        await self._send_via_conv_ref(chat_id, activity, activity)
