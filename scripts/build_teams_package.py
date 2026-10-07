"""Build an offline Hermes Teams package from owner-supplied branding, IDs and assets.

This validates bounded manifest inputs and PNG structure/dimensions. It does
not register identities, grant permissions, validate hosted legal pages, inspect
icon artwork, or upload the resulting package to Microsoft.
"""
from __future__ import annotations

import argparse
import io
import ipaddress
import json
from pathlib import Path
import re
import struct
from urllib.parse import urlsplit
import uuid
import zipfile
import zlib

MANIFEST_VERSION = "1.25"
MAX_ICON_BYTES = 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DEFAULT_DISPLAY_NAME = "Hermes Teams"
DEFAULT_SHORT_DESCRIPTION = "Work with channel files and structured updates using Hermes."
DEFAULT_FULL_DESCRIPTION = (
    "Hermes Teams connects Microsoft Teams to your Hermes assistant. "
    "Ask it to work with channel files, share documents, and post "
    "structured reports and status updates. Personal file delivery "
    "uses a consent prompt. Availability depends on your tenant "
    "permissions and configured channels."
)


def _guid(value: str, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value
    ):
        raise ValueError(f"{field} must be an actual canonical GUID")
    parsed = uuid.UUID(value)
    if not parsed.int:
        raise ValueError(f"{field} must not be the zero placeholder GUID")
    return str(parsed)


def _https_url(value: str, field: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise ValueError(f"{field} must be an HTTPS URL of at most 2048 characters")
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise ValueError(f"{field} contains invalid URL characters")
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        port = parts.port
        ascii_host = host.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        raise ValueError(f"{field} must have a valid HTTPS hostname") from None
    if parts.scheme.lower() != "https" or parts.username is not None or parts.password is not None:
        raise ValueError(f"{field} must use HTTPS without embedded credentials")
    if port not in (None, 443) or "." not in ascii_host or len(ascii_host) > 253:
        raise ValueError(f"{field} must use a public hostname and the HTTPS port")
    if ascii_host.endswith((".localhost", ".local", ".invalid", ".test", ".example")):
        raise ValueError(f"{field} must not use a local or reserved hostname")
    labels = ascii_host.split(".")
    if any(not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", x) for x in labels):
        raise ValueError(f"{field} must have a valid public hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError(f"{field} must use a hostname, not an IP address")
    # Legal/support pages should have durable public URLs, never signed links.
    if parts.query:
        raise ValueError(f"{field} must not contain query parameters")
    return value


def _manifest_text(value: str, field: str, maximum: int) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum
            or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(f"{field} must contain 1-{maximum} visible characters")
    return value


def build_manifest(*, bot_id: str, app_id: str, developer_name: str,
                   website_url: str, privacy_url: str, terms_url: str,
                   version: str = "1.0.0", display_name: str = DEFAULT_DISPLAY_NAME,
                   full_name: str | None = None,
                   short_description: str = DEFAULT_SHORT_DESCRIPTION,
                   full_description: str = DEFAULT_FULL_DESCRIPTION) -> dict:
    """Return a minimal Teams manifest with validated owner-supplied identity and copy."""
    bot_id = _guid(bot_id, "bot_id")
    app_id = _guid(app_id, "app_id")
    developer_name = _manifest_text(developer_name, "developer_name", 32)
    display_name = _manifest_text(display_name, "display_name", 30)
    full_name = _manifest_text(display_name if full_name is None else full_name, "full_name", 100)
    short_description = _manifest_text(short_description, "short_description", 80)
    full_description = _manifest_text(full_description, "full_description", 4000)
    if (not isinstance(version, str) or len(version) > 256
            or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version)):
        raise ValueError("version must have three numeric components, for example 1.0.0")
    return {
        "$schema": f"https://developer.microsoft.com/json-schemas/teams/v{MANIFEST_VERSION}/MicrosoftTeams.schema.json",
        "manifestVersion": MANIFEST_VERSION,
        "version": version,
        "id": app_id,
        "developer": {
            "name": developer_name,
            "websiteUrl": _https_url(website_url, "website_url"),
            "privacyUrl": _https_url(privacy_url, "privacy_url"),
            "termsOfUseUrl": _https_url(terms_url, "terms_url"),
        },
        "name": {"short": display_name, "full": full_name},
        "description": {
            "short": short_description,
            "full": full_description,
        },
        "icons": {"color": "color.png", "outline": "outline.png"},
        "accentColor": "#FFFFFF",
        "bots": [{"botId": bot_id, "scopes": ["personal", "team"],
                  "supportsFiles": True, "isNotificationOnly": False}],
        "supportsChannelFeatures": "tier1",
        "webApplicationInfo": {"id": bot_id, "resource": f"api://{bot_id}"},
        "authorization": {"permissions": {"resourceSpecific": [
            {"name": "ChannelMessage.Read.Group", "type": "Application"}
        ]}},
    }


def _read_icon(path: Path, size: int) -> bytes:
    """Check complete PNG chunks/CRCs and dimensions without decoding artwork."""
    with path.open("rb") as handle:
        data = handle.read(MAX_ICON_BYTES + 1)
    if len(data) > MAX_ICON_BYTES:
        raise ValueError(f"{size}px icon exceeds the 1 MiB input limit")
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError(f"{size}px icon must be a PNG file")
    offset, chunks, found_data, found_end = 8, 0, False, False
    while offset < len(data):
        if len(data) - offset < 12:
            raise ValueError(f"{size}px icon has truncated PNG chunks")
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError(f"{size}px icon has truncated PNG data")
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:end - 4]
        checksum = struct.unpack_from(">I", data, end - 4)[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != checksum:
            raise ValueError(f"{size}px icon has an invalid PNG checksum")
        if chunks == 0:
            if kind != b"IHDR" or length != 13:
                raise ValueError(f"{size}px icon requires a valid PNG header")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            allowed_depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if (width, height) != (size, size):
                raise ValueError(f"icon must measure {size}x{size} pixels")
            if depth not in allowed_depths.get(color, ()) or compression or filtering or interlace not in (0, 1):
                raise ValueError(f"{size}px icon has an invalid PNG header")
        elif kind == b"IHDR":
            raise ValueError(f"{size}px icon has duplicate PNG headers")
        if kind == b"IDAT":
            found_data = found_data or bool(payload)
        if kind == b"IEND":
            if length or end != len(data):
                raise ValueError(f"{size}px icon has an invalid PNG ending")
            found_end = True
            break
        chunks += 1
        offset = end
    if not found_data or not found_end:
        raise ValueError(f"{size}px icon is missing PNG image data or ending")
    return data


def build_package(*, color_icon: str | Path, outline_icon: str | Path,
                  output: str | Path, **manifest_inputs) -> Path:
    """Write exactly three package files; never overwrite an existing output."""
    manifest = build_manifest(**manifest_inputs)
    color = _read_icon(Path(color_icon), 192)
    outline = _read_icon(Path(outline_icon), 32)
    destination = Path(output)
    if destination.suffix.lower() != ".zip":
        raise ValueError("output must have a .zip suffix")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name, content in (
            ("manifest.json", (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")),
            ("color.png", color), ("outline.png", outline),
        ):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            package.writestr(info, content)
    # Validate everything before opening the output. Existing files are safe.
    with destination.open("xb") as handle:
        handle.write(buffer.getvalue())
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot-id", required=True, help="New Entra application/client GUID")
    parser.add_argument("--app-id", required=True, help="New Teams application/package GUID")
    parser.add_argument("--developer-name", required=True)
    parser.add_argument("--display-name", default=DEFAULT_DISPLAY_NAME, help="Short app name, up to 30 characters")
    parser.add_argument("--full-name", help="Full app name, up to 100 characters; defaults to the short name")
    parser.add_argument("--short-description", default=DEFAULT_SHORT_DESCRIPTION, help="App summary, up to 80 characters")
    parser.add_argument("--full-description", default=DEFAULT_FULL_DESCRIPTION, help="App description, up to 4000 characters")
    parser.add_argument("--website-url", required=True)
    parser.add_argument("--privacy-url", required=True)
    parser.add_argument("--terms-url", required=True)
    parser.add_argument("--color-icon", required=True, help="Owner-supplied 192x192 PNG")
    parser.add_argument("--outline-icon", required=True, help="Owner-supplied 32x32 PNG")
    parser.add_argument("--output", required=True, help="New ZIP path; its parent directory must exist")
    parser.add_argument("--version", default="1.0.0")
    args = parser.parse_args(argv)
    try:
        destination = build_package(**vars(args))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"Created {destination} (manifest {MANIFEST_VERSION}); review in Teams Developer Portal before installation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
