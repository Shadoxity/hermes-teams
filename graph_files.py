"""Profile-local Microsoft Graph channel file transfers.

Design references: NousResearch/hermes-agent PRs #118103/#118127 (tournierjc),
pinned in upstream/. Copyright (c) 2025 Nous Research; MIT, see LICENSE.
This implementation deliberately avoids their organization sharing links,
group-chat filesFolder assumption, and edits to Hermes's shared Graph client.

Graph credentials are supplied explicitly. Preauthenticated transfer URLs never
receive a Graph bearer token; production transfers use Hermes's SSRF-safe client.
"""

from __future__ import annotations

import asyncio
import ipaddress
import mimetypes
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

GRAPH_ORIGIN = "https://graph.microsoft.com"
GRAPH_BASE = GRAPH_ORIGIN + "/v1.0"
SIMPLE_UPLOAD_MAX_BYTES = 4 * 1024 * 1024  # Deliberate local threshold, not Graph's API maximum.
UPLOAD_CHUNK_BYTES = 10 * 320 * 1024  # Non-final fragments must be multiples of 320 KiB.
DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
_FILE_HOST_SUFFIXES = (".sharepoint.com", ".sharepoint-df.com", ".1drv.com", ".onedrive.com")


class GraphFileError(RuntimeError):
    """Safe to show in a channel: excludes tokens, URLs and remote error bodies."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class DownloadedFile:
    name: str
    data: bytes
    content_type: str

    @property
    def filename(self) -> str:
        return self.name

    @property
    def mime(self) -> str:
        return self.content_type


def safe_graph_filename(name: str) -> str:
    """A single readable filename, never a path or a driveItem addressing expression."""
    name = str(name or "file").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name).strip(" .")
    return (name or "file")[:200]


def _segment(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "/\\?#\r\n"):
        raise GraphFileError("A required Microsoft Graph identifier is missing or invalid.")
    return quote(value.strip(), safe="")


def _path(value: str) -> str:
    parts = value.strip("/").split("/")
    if not parts or any(p in ("", ".", "..") or "\\" in p or any(ord(c) < 32 for c in p) for p in parts):
        raise GraphFileError("The SharePoint file path is invalid.")
    return "/".join(quote(p, safe="") for p in parts)


def _https_url(value: str, *, file_host: bool = False):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.port not in (None, 443) or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment or "\\" in value
                or any(ord(c) < 32 for c in value)):
            raise ValueError
        host = parsed.hostname.lower()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
        if file_host and not any(host.endswith(s) and len(host) > len(s) for s in _FILE_HOST_SUFFIXES):
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise GraphFileError("The file service returned an unsafe or unsupported URL.") from None


def _graph_url(path: str) -> str:
    if path.startswith("https://"):
        parsed = _https_url(path)
        if parsed.hostname != "graph.microsoft.com" or not parsed.path.startswith("/v1.0/"):
            raise GraphFileError("Refusing to send Graph credentials outside Microsoft Graph.")
        return path
    if not path.startswith("/") or path.startswith("//") or "\\" in path or "#" in path:
        raise GraphFileError("The Microsoft Graph request path is invalid.")
    return GRAPH_BASE + path


def _error(status: int, operation: str) -> GraphFileError:
    if status in (401, 403):
        detail = "Access was denied; check this profile's Graph consent and site access."
    elif status == 404:
        detail = "The requested channel, message or file was not found or is not accessible."
    elif status == 429:
        detail = "Microsoft throttled the request; retry later."
    else:
        detail = "The file operation did not complete."
    return GraphFileError(f"{operation} failed (HTTP {status}). {detail}", status_code=status)


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    raw = response.headers.get("Retry-After", "") if response else ""
    try:
        delay = float(raw)
    except ValueError:
        try:
            date = parsedate_to_datetime(raw)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = (date - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            delay = float(2**attempt)
    return max(0.0, min(delay, 30.0))


class GraphFileClient:
    """One instance per profile; injected clients remain owned by the caller.

    ``token_provider`` implements async ``get_access_token(force_refresh=False)``.
    Injected transfer clients are trusted testing/transport seams. In production,
    omit ``transfer_client`` to enforce DNS-level SSRF checks via Hermes.
    """

    def __init__(self, token_provider: Any, *, graph_client: httpx.AsyncClient | None = None,
                 transfer_client: httpx.AsyncClient | None = None,
                 max_file_bytes: int = DEFAULT_MAX_FILE_BYTES, max_retries: int = 2,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep):
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        self._token_provider = token_provider
        self._graph = graph_client
        self._transfer = transfer_client
        self._own_graph = graph_client is None
        self._own_transfer = transfer_client is None
        self.max_file_bytes = max_file_bytes
        self.max_retries = max(0, min(max_retries, 4))
        self._sleep = sleep

    @classmethod
    def from_profile(cls, scoped_getter: Callable[[str, str], str], *,
                     teams_credentials: tuple[str, str, str] = ("", "", ""), **kwargs):
        """Read the caller's scoped settings only; never inspect process-global env.

        The explicit tuple is ``(tenant_id, client_id, client_secret)``. A partial
        MSGRAPH identity is an error rather than a mixture of two identities.
        """
        values = tuple(str(scoped_getter(key, "") or "").strip() for key in (
            "MSGRAPH_TENANT_ID", "MSGRAPH_CLIENT_ID", "MSGRAPH_CLIENT_SECRET"))
        if any(values) and not all(values):
            raise GraphFileError("This profile has incomplete MSGRAPH credentials.")
        values = values if all(values) else tuple(str(v or "").strip() for v in teams_credentials)
        if len(values) != 3 or not all(values):
            raise GraphFileError("Graph file access is not configured for this profile.")
        from tools.microsoft_graph_auth import GraphCredentials, MicrosoftGraphTokenProvider
        return cls(MicrosoftGraphTokenProvider(GraphCredentials(*values)), **kwargs)

    async def aclose(self) -> None:
        if self._own_graph and self._graph is not None:
            await self._graph.aclose()
        if self._own_transfer and self._transfer is not None:
            await self._transfer.aclose()

    async def _graph_http(self) -> httpx.AsyncClient:
        if self._graph is None:
            self._graph = httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False)
        return self._graph

    async def _transfer_http(self) -> httpx.AsyncClient:
        if self._transfer is None:
            try:
                from tools.url_safety import create_ssrf_safe_async_client
            except ImportError:
                raise GraphFileError("Hermes's safe file-transfer transport is unavailable.") from None
            self._transfer = create_ssrf_safe_async_client(timeout=60, trust_env=False, follow_redirects=False)
        return self._transfer

    @staticmethod
    async def _read_limited(response: httpx.Response, limit: int) -> bytes:
        raw_size = response.headers.get("Content-Length", "")
        if raw_size.isdigit() and int(raw_size) > limit:
            raise GraphFileError("The file or response exceeds the configured size limit.")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > limit:
                raise GraphFileError("The file or response exceeds the configured size limit.")
            body.extend(chunk)
        return bytes(body)

    async def _request(self, method: str, path: str, *, params=None, json=None, content=None,
                       content_type: str | None = None) -> dict[str, Any]:
        url = _graph_url(path)  # Validate BEFORE asking for a token.
        client = await self._graph_http()
        refresh = False
        for attempt in range(self.max_retries + 1):
            response = None
            try:
                try:
                    token = await self._token_provider.get_access_token(force_refresh=refresh)
                except Exception:
                    raise GraphFileError("Graph authentication failed for this profile.") from None
                headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
                if content_type:
                    headers["Content-Type"] = content_type
                request = client.build_request(method, url, params=params, json=json, content=content, headers=headers)
                request.headers.pop("Cookie", None)
                response = await client.send(request, auth=None, follow_redirects=False, stream=True)
                status = response.status_code
                if status == 401 and attempt < self.max_retries:
                    refresh = True
                elif status == 429 or (method == "GET" and status >= 500):
                    if attempt == self.max_retries:
                        raise _error(status, "Microsoft Graph")
                elif not 200 <= status < 300:
                    raise _error(status, "Microsoft Graph")
                else:
                    raw = await self._read_limited(response, MAX_METADATA_BYTES)
                    if status == 204 or not raw:
                        return {}
                    try:
                        import json as json_module
                        payload = json_module.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        raise GraphFileError("Microsoft Graph returned invalid metadata.") from None
                    if not isinstance(payload, dict):
                        raise GraphFileError("Microsoft Graph returned unexpected metadata.")
                    return payload
            except httpx.HTTPError:
                # Retrying an ambiguously completed rename upload could duplicate files.
                if method != "GET" or attempt == self.max_retries:
                    raise GraphFileError("Microsoft Graph connection failed; the operation may need checking before retrying.") from None
            finally:
                if response is not None:
                    await response.aclose()
            await self._sleep(_retry_delay(response, attempt))
        raise GraphFileError("Microsoft Graph retry limit reached.")

    async def get_message_attachments(self, team_id: str, channel_id: str, message_id: str,
                                      root_id: str | None = None) -> list[dict[str, Any]]:
        base = f"/teams/{_segment(team_id)}/channels/{_segment(channel_id)}/messages"
        path = f"{base}/{_segment(root_id)}/replies/{_segment(message_id)}" if root_id and root_id != message_id else f"{base}/{_segment(message_id)}"
        payload = await self._request("GET", path)
        attachments = payload.get("attachments") or []
        return [a for a in attachments if isinstance(a, dict) and a.get("contentType") == "reference"]

    async def get_thread_attachments(self, team_id: str, channel_id: str, root_id: str,
                                      *, max_messages: int = 100, max_attachments: int = 20) -> list[dict[str, Any]]:
        """Explicit bounded retrieval; never observes or stores unmentioned chatter.

        Includes the root and at most ``max_messages - 1`` replies, in the order
        returned by Graph. Additional files beyond the hard cap are not fetched.
        """
        max_messages = max(1, min(max_messages, 100))
        max_attachments = max(1, min(max_attachments, 20))
        base = f"/teams/{_segment(team_id)}/channels/{_segment(channel_id)}/messages/{_segment(root_id)}"
        root = await self._request("GET", base)
        files: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def append_from(message):
            if not isinstance(message, dict):
                return
            for attachment in message.get("attachments") or []:
                if not isinstance(attachment, dict) or attachment.get("contentType") != "reference":
                    continue
                key = (str(attachment.get("contentUrl") or ""), str(attachment.get("id") or ""))
                if key in seen:
                    continue
                seen.add(key)
                files.append(dict(attachment, _message_id=message.get("id", root_id), _root_id=root_id))
                if len(files) == max_attachments:
                    return

        append_from(root)
        messages_read = 1
        next_path = base + "/replies?$top=50"
        # An empty page with a repeated nextLink must not bypass the message cap.
        for _ in range(4):
            if messages_read >= max_messages or len(files) >= max_attachments:
                break
            page = await self._request("GET", next_path)
            for message in page.get("value") or []:
                if messages_read >= max_messages or len(files) >= max_attachments:
                    break
                append_from(message)
                messages_read += 1
            next_path = page.get("@odata.nextLink")
            if not next_path:
                break
        return files

    async def get_channel_folder(self, team_id: str, channel_id: str) -> dict[str, Any]:
        folder = await self._request("GET", f"/teams/{_segment(team_id)}/channels/{_segment(channel_id)}/filesFolder")
        if not folder.get("id") or not (folder.get("parentReference") or {}).get("driveId"):
            raise GraphFileError("Teams did not provide this channel's file storage identifiers.")
        return folder

    async def get_drive_item(self, drive_id: str, item_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/drives/{_segment(drive_id)}/items/{_segment(item_id)}")

    @staticmethod
    def _reference_path(url: str) -> tuple[str, str]:
        parsed = _https_url(url, file_host=True)
        if not parsed.hostname.endswith((".sharepoint.com", ".sharepoint-df.com")):
            raise GraphFileError("This reference is not a supported SharePoint path URL; supply drive/item identifiers.")
        path = unquote(parsed.path)
        path = re.sub(r"^/:[A-Za-z]:/r/", "/", path)
        if "/_layouts/" in path.lower() or re.match(r"^/:[A-Za-z]:/", path):
            raise GraphFileError("This sharing link has no resolvable file path; supply the file's direct URL or drive/item identifiers.")
        _path(path)
        return parsed.hostname.lower(), path

    async def _item_in_drive(self, drive_id: str, root_url: str, host: str, file_path: str):
        root_host, root_path = self._reference_path(root_url)
        if root_host != host or not file_path.casefold().startswith(root_path.rstrip("/").casefold() + "/"):
            return None
        relative = file_path[len(root_path.rstrip("/")) + 1:]
        item = await self._request("GET", f"/drives/{_segment(drive_id)}/root:/{_path(relative)}")
        item.setdefault("parentReference", {}).setdefault("driveId", drive_id)
        return item

    async def resolve_reference(self, url: str, team_id: str, channel_id: str) -> dict[str, Any]:
        """Resolve path URLs through authorized channel/site drives, never /shares.

        Sites.Selected works with already granted sites; this method does not
        grant access or enumerate the tenant. Opaque sharing links fail clearly.
        """
        host, file_path = self._reference_path(url)
        folder = await self.get_channel_folder(team_id, channel_id)
        folder_host, _ = self._reference_path(folder.get("webUrl", ""))
        if host != folder_host:
            raise GraphFileError("The file reference belongs to a different SharePoint host than this channel.")
        drive_id = folder["parentReference"]["driveId"]
        root = await self._request("GET", f"/drives/{_segment(drive_id)}/root", params={"$select": "id,webUrl"})
        item = await self._item_in_drive(drive_id, root.get("webUrl", ""), host, file_path)
        if item is not None:
            return item
        parts = file_path.strip("/").split("/")
        if len(parts) < 3 or parts[0].lower() not in ("sites", "teams"):
            raise GraphFileError("The reference is outside this channel's drive and has no supported site path.")
        site = await self._request("GET", f"/sites/{host}:/{_path('/'.join(parts[:2]))}", params={"$select": "id"})
        next_path = f"/sites/{_segment(site.get('id', ''))}/drives"
        for _ in range(5):
            page = await self._request("GET", next_path)
            for drive in page.get("value", []):
                if not isinstance(drive, dict) or not drive.get("id") or not drive.get("webUrl"):
                    continue
                item = await self._item_in_drive(drive["id"], drive["webUrl"], host, file_path)
                if item is not None:
                    return item
            next_path = page.get("@odata.nextLink")
            if not next_path:
                break
        raise GraphFileError("The reference could not be resolved within the authorized SharePoint drives.")

    async def download_attachment(self, attachment: Mapping[str, Any] | str, *,
                                  team_id: str, channel_id: str) -> DownloadedFile:
        if isinstance(attachment, str):
            item = await self.resolve_reference(attachment, team_id, channel_id)
        else:
            drive_id = attachment.get("driveId") or (attachment.get("parentReference") or {}).get("driveId")
            item_id = attachment.get("itemId") or (attachment.get("id") if drive_id else None)
            if drive_id and item_id:
                item = await self.get_drive_item(drive_id, item_id)
            elif attachment.get("@microsoft.graph.downloadUrl"):
                item = dict(attachment)
            else:
                item = await self.resolve_reference(attachment.get("contentUrl") or attachment.get("webUrl") or "", team_id, channel_id)
        size = item.get("size")
        if isinstance(size, (int, float)) and size > self.max_file_bytes:
            raise GraphFileError("The file exceeds the configured size limit.")
        url = item.get("@microsoft.graph.downloadUrl")
        if not url:
            raise GraphFileError("Microsoft Graph did not return a downloadable file URL.")
        data, content_type = await self._download(url)
        name = safe_graph_filename(item.get("name") or "file")
        mime = (item.get("file") or {}).get("mimeType") or content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        return DownloadedFile(name, data, mime)

    async def _open_transfer(self, method: str, url: str, *, content=None, headers=None):
        _https_url(url, file_host=True)
        client = await self._transfer_http()
        request = client.build_request(method, url, content=content, headers=headers)
        # Also remove any accidental defaults from an injected client.
        request.headers.pop("Authorization", None)
        request.headers.pop("Cookie", None)
        try:
            return await client.send(request, auth=None, follow_redirects=False, stream=True)
        except ValueError:
            # The production SSRF guard can reject DNS results with a URL-bearing
            # error; do not expose a preauthenticated URL in the user-facing text.
            raise GraphFileError("The file-transfer destination failed its safety check.") from None

    async def _download(self, url: str) -> tuple[bytes, str]:
        current = url
        for _ in range(4):
            redirect = None
            for attempt in range(self.max_retries + 1):
                response = None
                try:
                    response = await self._open_transfer("GET", current)
                    status = response.status_code
                    if status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            raise GraphFileError("The download redirect did not include a destination.")
                        redirect = urljoin(current, location)
                        _https_url(redirect, file_host=True)
                        break
                    if status == 429 or status >= 500:
                        if attempt == self.max_retries:
                            raise _error(status, "File download")
                    elif status != 200:
                        raise _error(status, "File download")
                    else:
                        return await self._read_limited(response, self.max_file_bytes), response.headers.get("Content-Type", "").split(";", 1)[0]
                except httpx.HTTPError:
                    if attempt == self.max_retries:
                        raise GraphFileError("The file download connection failed.") from None
                finally:
                    if response is not None:
                        await response.aclose()
                await self._sleep(_retry_delay(response, attempt))
            if redirect is None:
                break
            current = redirect
        raise GraphFileError("The file download exceeded the redirect or retry limit.")

    async def _upload_chunk(self, url: str, chunk: bytes, start: int, total: int) -> tuple[int, dict]:
        headers = {"Content-Type": "application/octet-stream", "Content-Length": str(len(chunk)),
                   "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{total}"}
        for attempt in range(self.max_retries + 1):
            response = None
            try:
                response = await self._open_transfer("PUT", url, content=chunk, headers=headers)
                status = response.status_code
                if status == 429 or status >= 500:
                    if attempt == self.max_retries:
                        raise _error(status, "File upload")
                elif status not in (200, 201, 202):
                    raise _error(status, "File upload")
                else:
                    raw = await self._read_limited(response, MAX_METADATA_BYTES)
                    try:
                        import json
                        payload = json.loads(raw)
                        if not isinstance(payload, dict):
                            raise ValueError
                    except (ValueError, UnicodeDecodeError):
                        raise GraphFileError("The upload service returned invalid metadata.") from None
                    return status, payload
            except httpx.HTTPError:
                # The range is replayable, but the final response can be ambiguous.
                if attempt == self.max_retries:
                    raise GraphFileError("The upload connection failed; check the channel before retrying.") from None
            finally:
                if response is not None:
                    await response.aclose()
            await self._sleep(_retry_delay(response, attempt))
        raise GraphFileError("The file upload retry limit was reached.")

    async def upload_channel_file(self, team_id: str, channel_id: str, path: str | Path,
                                   filename: str | None = None) -> dict[str, Any]:
        local = Path(path)
        try:
            size = local.stat().st_size
            if not local.is_file():
                raise OSError
        except OSError:
            raise GraphFileError("The local file could not be opened.") from None
        if size > self.max_file_bytes:
            raise GraphFileError("The file exceeds the configured upload size limit.")
        name = safe_graph_filename(filename or local.name)
        folder = await self.get_channel_folder(team_id, channel_id)
        drive_id = folder["parentReference"]["driveId"]
        target = f"/drives/{_segment(drive_id)}/items/{_segment(folder['id'])}:/{quote(name, safe='')}"
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        try:
            with local.open("rb") as handle:
                if size <= SIMPLE_UPLOAD_MAX_BYTES:
                    data = handle.read(SIMPLE_UPLOAD_MAX_BYTES + 1)
                    if len(data) != size:
                        raise GraphFileError("The local file changed while preparing the upload.")
                    item = await self._request("PUT", target + ":/content", content=data, content_type=mime,
                                               params={"@microsoft.graph.conflictBehavior": "rename"})
                else:
                    session = await self._request("POST", target + ":/createUploadSession",
                                                  json={"item": {"@microsoft.graph.conflictBehavior": "rename", "name": name}})
                    upload_url = session.get("uploadUrl", "")
                    _https_url(upload_url, file_host=True)
                    offset, item = 0, None
                    while offset < size:
                        chunk = handle.read(min(UPLOAD_CHUNK_BYTES, size - offset))
                        if not chunk:
                            raise GraphFileError("The local file changed during upload.")
                        status, result = await self._upload_chunk(upload_url, chunk, offset, size)
                        offset += len(chunk)
                        if status in (200, 201):
                            if offset != size:
                                raise GraphFileError("The upload service finished before all file bytes were sent.")
                            item = result
                        elif result.get("nextExpectedRanges") != [f"{offset}-"]:
                            raise GraphFileError("The upload service requested an unexpected byte range; retry the file transfer.")
                    if handle.read(1):
                        raise GraphFileError("The local file changed during upload.")
                    if item is None:
                        raise GraphFileError("The upload service did not confirm the completed file.")
        except OSError:
            raise GraphFileError("The local file could not be read during upload.") from None
        if not item.get("id") or not item.get("webUrl"):
            raise GraphFileError("The upload response did not include a usable file link; check the channel before retrying.")
        _https_url(item["webUrl"], file_host=True)
        item.setdefault("parentReference", {}).setdefault("driveId", drive_id)
        item.setdefault("name", name)
        return item
