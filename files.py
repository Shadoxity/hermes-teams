"""Channel file and rich-post integration for the external Teams adapter.

Based on NousResearch/hermes-agent PR #118103 (MIT); channel Graph operations
are local to this plugin and preserve existing SharePoint permissions.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from .cards import build_card, render_fallback
from .transport import flat_conversation_id
from .posting import TeamsPostingMixin, PostingError

log = logging.getLogger(__name__)
_TEAM_GUID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_CONNECTOR_TEAM_ID_RE = re.compile(r"[A-Za-z0-9:@_.-]{1,512}\Z")
_TEAM_LOOKUP_TIMEOUT_SECONDS = 5.0
_TEAM_LOOKUP_CACHE_MAX = 128
_TEAM_LOOKUP_SUCCESS_TTL = 3600.0
_TEAM_LOOKUP_FAILURE_TTL = 60.0


class FriendlyFileError(ValueError):
    """An intentionally safe message for the agent/user, with no remote response body."""


def field(obj, *names, default=None):
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value is not None:
            return value
    return default


@dataclass(frozen=True)
class ChannelTarget:
    team_id: str
    channel_id: str


def thread_root(chat_id: str, reply_to=None):
    parts = re.split(r";messageid=", chat_id, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2:
        return parts[1]
    return str(reply_to) if reply_to else None


def extract_target(activity):
    data = field(activity, "channel_data", "channelData", default={})
    team = field(data, "team", default={})
    team_id = field(team, "aad_group_id", "aadGroupId") or field(data, "teamAadGroupId", "team_aad_group_id")
    if not team_id:
        candidate = str(field(team, "id", default=""))
        if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", candidate):
            team_id = candidate
    channel_id = field(field(data, "channel", default={}), "id")
    if team_id and channel_id:
        return ChannelTarget(str(team_id), str(channel_id))
    return None


class TeamsFilesMixin(TeamsPostingMixin):
    def _init_teams_files(self):
        from .runtime import profile_home
        self._graph_files = None
        self._file_targets = {}
        self._team_detail_cache = {}
        self._team_detail_inflight = {}
        self._targets_file = Path(profile_home()) / "plugin-data" / "hermes-teams" / "channels.json"
        if self._targets_file.exists():
            try:
                data = json.loads(self._targets_file.read_text())
                self._file_targets = {k: ChannelTarget(**v) for k, v in data.items()}
            except (ValueError, TypeError, OSError):
                log.warning("Stored Teams channel mapping could not be loaded")
        for chat, data in self._extra.get("file_targets", {}).items():
            self._file_targets[flat_conversation_id(str(chat))] = ChannelTarget(**data)

    def _remember_file_context(self, activity):
        target = extract_target(activity)
        chat_id = field(field(activity, "conversation"), "id")
        if not target or not chat_id:
            return
        self._store_file_target(chat_id, target)

    def _store_file_target(self, chat_id, target):
        key = flat_conversation_id(str(chat_id))
        if self._file_targets.get(key) == target:
            return
        self._file_targets[key] = target
        if len(self._file_targets) > 1000:
            self._file_targets.pop(next(iter(self._file_targets)))
        try:
            self._targets_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._targets_file.with_suffix(".tmp")
            temporary.write_text(json.dumps({k: asdict(v) for k, v in self._file_targets.items()}))
            temporary.replace(self._targets_file)
        except OSError:
            log.warning("Teams channel mapping is available in memory but could not be persisted")

    async def _ensure_file_context(self, ctx):
        """Resolve a channel route AFTER the caller authorizes the inbound activity.

        SDK 2.0.13.4 exposes ``ctx.api.teams.get_by_id(id) -> TeamDetails``.
        Only the exact validated Bot Framework team ID from that activity is
        supplied; no user URL or Graph directory discovery is involved. Positive
        and negative caches belong to this adapter/profile, and concurrent
        lookups of the same team share one bounded request.
        """
        activity = field(ctx, "activity")
        conversation = field(activity, "conversation")
        if field(conversation, "conversation_type", "conversationType") != "channel":
            return None
        chat_id = field(conversation, "id")
        data = field(activity, "channel_data", "channelData", default={})
        channel_id = field(field(data, "channel", default={}), "id")
        if not isinstance(chat_id, str) or not isinstance(channel_id, str) or not channel_id:
            return None
        target = extract_target(activity)
        if target and _TEAM_GUID_RE.fullmatch(target.team_id):
            self._store_file_target(chat_id, target)
            return target
        known = self._file_targets.get(flat_conversation_id(chat_id))
        if known and known.channel_id == channel_id and _TEAM_GUID_RE.fullmatch(known.team_id):
            return known
        team_id = field(field(data, "team", default={}), "id")
        if (not isinstance(team_id, str) or not _CONNECTOR_TEAM_ID_RE.fullmatch(team_id)
                or team_id in (".", "..")):
            return None
        cached = self._team_detail_cache.get(team_id)
        if cached and cached[0] > time.monotonic():
            group_id = cached[1]
        else:
            task = self._team_detail_inflight.get(team_id)
            if task is None:
                lookup = field(field(field(ctx, "api"), "teams"), "get_by_id")
                if not callable(lookup) or len(self._team_detail_inflight) >= _TEAM_LOOKUP_CACHE_MAX:
                    return None

                async def lookup_and_cache():
                    group_id = None
                    try:
                        details = await asyncio.wait_for(lookup(team_id), timeout=_TEAM_LOOKUP_TIMEOUT_SECONDS)
                        value = field(details, "aad_group_id", "aadGroupId")
                        returned_id = field(details, "id")
                        if (isinstance(value, str) and _TEAM_GUID_RE.fullmatch(value)
                                and returned_id in (None, team_id)):
                            group_id = value
                        else:
                            log.warning("Teams team details did not contain a valid matching Graph group ID")
                    except Exception as exc:
                        # SDK errors may contain request URLs or credentials.
                        log.warning("Teams team details lookup unavailable (%s)", type(exc).__name__)
                    finally:
                        self._team_detail_inflight.pop(team_id, None)
                    ttl = _TEAM_LOOKUP_SUCCESS_TTL if group_id else _TEAM_LOOKUP_FAILURE_TTL
                    if team_id not in self._team_detail_cache and len(self._team_detail_cache) >= _TEAM_LOOKUP_CACHE_MAX:
                        self._team_detail_cache.pop(next(iter(self._team_detail_cache)))
                    self._team_detail_cache[team_id] = (time.monotonic() + ttl, group_id)
                    return group_id

                task = asyncio.create_task(lookup_and_cache())
                self._team_detail_inflight[team_id] = task
            group_id = await asyncio.shield(task)
        if not group_id:
            return None
        target = ChannelTarget(group_id, channel_id)
        self._store_file_target(chat_id, target)
        return target

    def _file_target(self, chat_id):
        target = self._file_targets.get(flat_conversation_id(str(chat_id)))
        if target is None:
            raise FriendlyFileError("Channel file location is unknown. Mention the bot in this channel first, or configure file_targets.")
        return target

    def _file_client(self):
        if self._graph_files is None:
            from .graph_files import GraphFileClient
            from gateway.platforms._shared import get_scoped_secret
            self._graph_files = GraphFileClient.from_profile(
                get_scoped_secret,
                teams_credentials=(self._tenant_id, self._client_id, self._client_secret),
                max_file_bytes=int(self._extra.get("max_file_bytes", 100 * 1024 * 1024)),
            )
        return self._graph_files

    async def _cache_channel_reference(self, attachment, target):
        from gateway.platforms.base import cache_media_bytes_async
        result = await self._file_client().download_attachment(
            attachment, team_id=target.team_id, channel_id=target.channel_id)
        cached = await cache_media_bytes_async(result.data, filename=result.name, mime_type=result.content_type)
        if cached is None:
            raise FriendlyFileError("The attachment file type is not supported by the Hermes media cache")
        return cached.path, cached.media_type, cached.kind

    async def _collect_teams_media(self, activity):
        attachments = field(activity, "attachments", default=[]) or []
        conversation = field(activity, "conversation")
        target = None
        if field(conversation, "conversation_type", "conversationType") == "channel":
            target = extract_target(activity)
            if target is None or not _TEAM_GUID_RE.fullmatch(target.team_id):
                chat_id = field(conversation, "id", default="")
                target = self._file_targets.get(flat_conversation_id(str(chat_id)))
        media, errors, seen = [], [], set()
        for attachment in attachments:
            kind = str(field(attachment, "content_type", "contentType", default="")).lower()
            url = str(field(attachment, "content_url", "contentUrl", default=""))
            is_reference = kind == "reference" or (
                bool(url) and (urlparse(url).hostname or "").endswith(".sharepoint.com")
                and kind != "application/vnd.microsoft.teams.file.download.info")
            if is_reference:
                if not target:
                    errors.append("File reference requires a Teams channel with a known SharePoint location.")
                    continue
                try:
                    mapped = {"contentUrl": url, "contentType": kind,
                              "name": field(attachment, "name", default="file")}
                    media.append(await self._cache_channel_reference(mapped, target))
                    seen.add(url)
                except Exception as exc:
                    errors.append(self._file_error(exc))
            else:
                cached = await self._cache_attachment(attachment)
                if cached:
                    media.append(cached)
        # Teams sometimes delivers only an HTML body with attachment placeholders.
        # Fetch exactly the triggering message/reply; do not observe unrelated chatter.
        text = str(field(activity, "text", default=""))
        html_files = any("<attachment" in str(field(a, "content", default="")) for a in attachments)
        html_only = bool(attachments) and all(
            str(field(a, "content_type", "contentType", default="")).lower() in ("text/html", "text/plain")
            and not field(a, "content_url", "contentUrl")
            and not field(a, "name")
            for a in attachments)
        missing_file_url = any(
            field(a, "content_type", "contentType") == "application/vnd.microsoft.teams.file.download.info"
            and not field(field(a, "content", default={}), "downloadUrl", "download_url")
            for a in attachments)
        if target and ("<attachment" in text or html_files or html_only or missing_file_url):
            try:
                chat_id = str(field(field(activity, "conversation"), "id", default=""))
                message_id = str(field(activity, "id", default=""))
                root = thread_root(chat_id, field(activity, "reply_to_id", "replyToId"))
                refs = await self._file_client().get_message_attachments(
                    target.team_id, target.channel_id, message_id,
                    root_id=root if root != message_id else None)
                for attachment in refs:
                    url = attachment.get("contentUrl", "")
                    if url and url not in seen:
                        seen.add(url)
                        try:
                            media.append(await self._cache_channel_reference(attachment, target))
                        except Exception as exc:
                            errors.append(self._file_error(exc))
            except Exception as exc:
                errors.append(self._file_error(exc))
        return media, list(dict.fromkeys(errors))

    @staticmethod
    def _file_error(exc):
        # Only plugin exceptions are deliberately safe; HTTP exception strings can contain signed URLs.
        from .graph_files import GraphFileError
        if isinstance(exc, (GraphFileError, FriendlyFileError, PostingError)):
            return str(exc)[:500]
        return f"Teams operation failed ({type(exc).__name__}); delivery was not confirmed."

    async def download_message_files(self, chat_id, message_id, root_id=None, include_thread=False):
        target = self._file_target(chat_id)
        client = self._file_client()
        root = root_id or thread_root(chat_id)
        if include_thread:
            root = root_id or thread_root(chat_id) or message_id
            refs = await client.get_thread_attachments(target.team_id, target.channel_id, root)
        else:
            refs = await client.get_message_attachments(
                target.team_id, target.channel_id, message_id,
                root_id=root if root != message_id else None)
        result = []
        for attachment in refs:
            path, mime, kind = await self._cache_channel_reference(attachment, target)
            result.append({"path": path, "mime_type": mime, "kind": kind,
                           "name": attachment.get("name", Path(path).name)})
        return result

    async def send_rich_post(self, chat_id, post, reply_to=None):
        from gateway.platforms.base import SendResult
        try:
            card, fallback = build_card(post), render_fallback(post)
            # A card plus activity.text becomes two Teams activities and cannot
            # create one channel root. Keep accessible text in fallbackText and
            # the notification summary instead.
            payload = {"type": "message", "summary": fallback,
                       "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}
            result = await self._post_activity(chat_id, payload, reply_to=reply_to)
            message_id = field(result, "id")
            return SendResult(success=bool(message_id), message_id=message_id,
                              error=None if message_id else "Teams returned no message ID")
        except Exception as exc:
            return SendResult(success=False, error=self._file_error(exc))

    async def send_document(self, chat_id, file_path, caption=None, file_name=None, reply_to=None, metadata=None, **kwargs):
        from gateway.platforms.base import SendResult
        ref = self._conv_refs.get(chat_id)
        conv_type = field(field(ref, "conversation"), "conversation_type", "conversationType")
        if conv_type == "personal":
            return await self._send_file_consent(chat_id, file_path, caption=caption, file_name=file_name)
        try:
            target = self._file_target(chat_id)
            item = await self._file_client().upload_channel_file(
                target.team_id, target.channel_id, str(file_path).removeprefix("file://"), filename=file_name)
            sent = await self.send_rich_post(chat_id, {
                "template": "document", "title": item.get("name") or file_name or Path(file_path).name,
                "summary": caption or "File uploaded to this channel's SharePoint storage.",
                "actions": [{"label": "Open file", "url": item["webUrl"]}],
            }, reply_to=reply_to)
            if not sent.success:
                sent.error = "File uploaded, but the Teams post failed. File: " + item["webUrl"] + ". " + (sent.error or "")
            return sent
        except Exception as exc:
            return SendResult(success=False, error=self._file_error(exc))

    async def _post_activity(self, chat_id, payload, reply_to=None):
        from .adapter import _TEAMS_CONV_ID_RE
        base = flat_conversation_id(str(chat_id))
        root = thread_root(str(chat_id), reply_to)
        if not _TEAMS_CONV_ID_RE.fullmatch(base) or (root and not _TEAMS_CONV_ID_RE.fullmatch(root)):
            raise ValueError("Invalid Teams conversation or reply ID")
        if root:
            payload = {**payload, "replyToId": root}
        if self._is_new_channel_post(chat_id, reply_to):
            return await self._create_channel_post(chat_id, payload)
        if self._app:
            from microsoft_teams.api import MessageActivityInput
            activity = MessageActivityInput.model_validate(payload)
            # The full reference includes ;messageid= and retains the originating thread.
            if reply_to and not thread_root(str(chat_id)):
                result = await self._app.reply(chat_id, root, activity)
                self._remember_sent(result)
            else:
                result = await self._send_via_conv_ref(chat_id, activity, activity)
            return result
        service = self._transport_service_url_for(chat_id)
        conversation = base + (f";messageid={root}" if root else "")
        url = service + "v3/conversations/" + quote(conversation, safe="") + "/activities"
        return await self._request_activity_json(url, payload)
