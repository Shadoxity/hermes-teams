"""Small, validated Adaptive Card templates for Hermes channel posts.

``build_card`` returns card *content*, ready for an
``application/vnd.microsoft.card.adaptive`` attachment. ``render_fallback``
returns plain text for the same post. Neither function performs network I/O.
"""

from __future__ import annotations

import ipaddress
import json
import re
import unicodedata
from collections.abc import Mapping
from urllib.parse import parse_qsl, unquote, urlsplit


MAX_MESSAGE_BYTES = 28 * 1024
_TEMPLATES = {"report": "REPORT", "status": "STATUS UPDATE", "document": "DOCUMENT"}
_POST_FIELDS = {"template", "title", "summary", "status", "facts", "sections", "actions"}
_SECRET_KEYS = {
    "token", "accesstoken", "refreshtoken", "idtoken", "authtoken", "authkey",
    "authorization", "apikey", "clientsecret", "secret", "password", "sig",
    "signature", "code", "xamzcredential", "xamzsignature", "xamzsecuritytoken",
}
_STATUS_COLORS = {
    "complete": "Good", "completed": "Good", "done": "Good", "ready": "Good",
    "success": "Good", "successful": "Good", "on track": "Good",
    "pending": "Warning", "in progress": "Warning", "needs attention": "Warning",
    "warning": "Warning", "at risk": "Warning",
    "failed": "Attention", "error": "Attention", "blocked": "Attention",
}


def _mapping(value: object, field: str, allowed: set[str]) -> Mapping:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise ValueError(f"{field} contains an unsupported field")
    return value


def _text(value: object, field: str, limit: int, *, required: bool = False,
          multiline: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    # Normalize line endings, but reject invisible control characters and invalid
    # Unicode before these values reach JSON encoding or a Teams client.
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    allowed_controls = {"\n", "\t"} if multiline else set()
    if any(unicodedata.category(char) in {"Cc", "Cs"} and char not in allowed_controls
           for char in value):
        raise ValueError(f"{field} contains unsupported control characters")
    value = value.strip()
    if (required and not value) or len(value) > limit:
        raise ValueError(f"{field} must contain {'1' if required else '0'} to {limit} characters")
    return value


def _items(value: object, field: str, limit: int) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    if len(value) > limit:
        raise ValueError(f"{field} must contain at most {limit} items")
    return value


def _url(value: object, field: str) -> str:
    url = _text(value, field, 2048, required=True, multiline=False)
    if "\\" in url or any(char.isspace() for char in url):
        raise ValueError(f"{field} must be an HTTPS URL without whitespace")
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in unquote(url)):
        raise ValueError(f"{field} contains an encoded control character")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if (parts.scheme.lower() != "https" or not host or parts.username is not None
                or parts.password is not None or parts.port not in (None, 443)):
            raise ValueError
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
    except (ValueError, UnicodeError):
        raise ValueError(f"{field} must be an HTTPS URL without credentials") from None

    # This is syntactic destination validation, not a DNS lookup or permission
    # check. Rendering must not fetch caller-provided links or leak auth tokens.
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if (len(labels) < 2 or len(host) > 253
                or host.endswith((".localhost", ".local", ".internal", ".lan", ".home"))
                # Browser URL parsers may interpret noncanonical numeric hosts
                # as IPv4 (for example 0177.0.0.1 or 0x7f.0.0.1).
                or re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", labels[-1])
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                       for label in labels)):
            raise ValueError(f"{field} must use a public HTTPS host")
    else:
        if not address.is_global:
            raise ValueError(f"{field} must use a public HTTPS host")

    # The agent should publish durable SharePoint web URLs, never temporary
    # authenticated download/upload URLs or OAuth responses.
    for component in (parts.query, parts.fragment):
        for key, _ in parse_qsl(component, keep_blank_values=True):
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in _SECRET_KEYS or normalized.endswith("token"):
                raise ValueError(f"{field} must not contain credentials or signed access tokens")
    return url


def _normalize(post: Mapping) -> dict:
    post = _mapping(post, "post", _POST_FIELDS)
    template = _text(post.get("template", ""), "template", 16, required=True, multiline=False)
    if template not in _TEMPLATES:
        raise ValueError("template must be report, status, or document")
    result = {
        "template": template,
        "title": _text(post.get("title", ""), "title", 200, required=True, multiline=False),
        "summary": _text(post.get("summary", ""), "summary", 2000),
        "status": _text(post.get("status", ""), "status", 80, multiline=False),
        "facts": [], "sections": [], "actions": [],
    }
    for index, value in enumerate(_items(post.get("facts", []), "facts", 12)):
        field = f"facts[{index}]"
        item = _mapping(value, field, {"label", "value"})
        result["facts"].append({
            "label": _text(item.get("label", ""), f"{field}.label", 80, required=True, multiline=False),
            "value": _text(item.get("value", ""), f"{field}.value", 500, required=True),
        })
    for index, value in enumerate(_items(post.get("sections", []), "sections", 6)):
        field = f"sections[{index}]"
        item = _mapping(value, field, {"title", "text"})
        result["sections"].append({
            "title": _text(item.get("title", ""), f"{field}.title", 120, multiline=False),
            "text": _text(item.get("text", ""), f"{field}.text", 2000, required=True),
        })
    for index, value in enumerate(_items(post.get("actions", []), "actions", 3)):
        field = f"actions[{index}]"
        item = _mapping(value, field, {"label", "url"})
        result["actions"].append({
            "label": _text(item.get("label", ""), f"{field}.label", 64, required=True, multiline=False),
            "url": _url(item.get("url", ""), f"{field}.url"),
        })
    return result


def _markdown(text: str) -> str:
    # Cards accept a Markdown subset. User-supplied titles, values, and prose
    # remain literal rather than introducing links or changing the layout.
    return re.sub(r"([\\`*_{}\[\]()#+.!<>|~-])", r"\\\1", text)


def _text_block(text: str, **properties: object) -> dict:
    return {"type": "TextBlock", "text": _markdown(text), "wrap": True, **properties}


def _container(items: list[dict], *, style: str = "default", **properties: object) -> dict:
    """Group related content using host-provided colors and natural height."""
    return {"type": "Container", "style": style, "items": items, **properties}


def _status_block(status: str) -> dict:
    # Keep the status wording visible: color must never carry its meaning alone.
    return _text_block(f"Status: {status}", weight="Bolder", spacing="Small",
                       color=_STATUS_COLORS.get(status.casefold(), "Default"))


def _header(post: dict, *, style: str = "default", status: bool = True,
            summary: bool = False) -> dict:
    items = [
        _text_block(_TEMPLATES[post["template"]], size="Small", isSubtle=True),
        _text_block(post["title"], size="Large", weight="Bolder", spacing="Small"),
    ]
    if status and post["status"]:
        items.append(_status_block(post["status"]))
    if summary and post["summary"]:
        items.append(_text_block(post["summary"], spacing="Medium"))
    return _container(items, style=style)


def _fact_set(facts: list[dict]) -> dict:
    return {"type": "FactSet", "spacing": "Small", "facts": [
        {"title": _markdown(item["label"]), "value": _markdown(item["value"])}
        for item in facts
    ]}


def _fact_panel(facts: list[dict], title: str, *, style: str = "default",
                separator: bool = False) -> dict:
    return _container([
        _text_block(title, weight="Bolder"), _fact_set(facts),
    ], style=style, spacing="Medium", separator=separator)


def _section_blocks(sections: list[dict]) -> list[dict]:
    blocks = []
    for section in sections:
        items = []
        if section["title"]:
            items.append(_text_block(section["title"], weight="Bolder"))
        items.append(_text_block(section["text"], spacing="Small" if items else "Default"))
        blocks.append(_container(items, spacing="Medium", separator=True))
    return blocks


def _report_body(post: dict) -> list[dict]:
    body = [_header(post, style="accent")]
    if post["summary"]:
        body.append(_text_block(post["summary"], spacing="Medium"))
    if post["facts"]:
        body.append(_fact_panel(post["facts"], "At a glance", style="emphasis"))
    return body + _section_blocks(post["sections"])


def _status_body(post: dict) -> list[dict]:
    body = [_header(post, status=False)]
    callout = []
    if post["status"]:
        callout.append(_status_block(post["status"]))
    if post["summary"]:
        callout.append(_text_block(post["summary"], spacing="Small" if callout else "Default"))
    if callout:
        color = _STATUS_COLORS.get(post["status"].casefold(), "Default")
        style = color.lower() if color != "Default" else "emphasis"
        body.append(_container(callout, style=style, spacing="Medium"))
    if post["facts"]:
        body.append(_fact_panel(post["facts"], "Details", separator=True))
    return body + _section_blocks(post["sections"])


def _document_body(post: dict) -> list[dict]:
    body = [_header(post, style="emphasis", summary=True)]
    if post["facts"]:
        body.append(_fact_panel(post["facts"], "File details", separator=True))
    return body + _section_blocks(post["sections"])


def _fallback(post: dict) -> str:
    # Reading order and content stay stable across visual layouts.
    lines = [post["title"]]
    if post["status"]:
        lines.append(f"Status: {post['status']}")
    if post["summary"]:
        lines.append(post["summary"])
    if post["facts"]:
        lines.append("\n".join(f"{item['label']}: {item['value']}" for item in post["facts"]))
    for section in post["sections"]:
        lines.append(f"{section['title']}\n{section['text']}" if section["title"] else section["text"])
    if post["actions"]:
        lines.append("\n".join(f"{item['label']}: {item['url']}" for item in post["actions"]))
    return "\n\n".join(lines)


def _render(post: dict) -> tuple[dict, str]:
    body = {"report": _report_body, "status": _status_body,
            "document": _document_body}[post["template"]](post)
    fallback = _fallback(post)
    card = {
        "$schema": "https://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard", "version": "1.4", "body": body,
        "fallbackText": fallback,
    }
    if post["actions"]:
        card["actions"] = [
            {"type": "Action.OpenUrl", "title": item["label"], "url": item["url"]}
            for item in post["actions"]
        ]
    return card, fallback


def _prepare(post: Mapping) -> tuple[dict, str]:
    card, fallback = _render(_normalize(post))
    # Include the usual bot activity wrapper and accessible message text in the
    # budget, not only the card JSON; otherwise repeated fallback text could
    # push a seemingly small card beyond the transport's size limit.
    envelope = {"type": "message", "summary": fallback, "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "content": card,
    }]}
    if len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("post exceeds the 28 KiB message limit; shorten it or link to a document")
    return card, fallback


def build_card(post: Mapping) -> dict:
    """Validate structured post data and return an Adaptive Card 1.4 object.

    Required keys are ``template`` (report/status/document) and ``title``.
    Optional keys: summary, status, facts [{label, value}], sections
    [{title, text}], and actions [{label, url}]. Text fields are plain text.
    Actions must use durable public-host HTTPS URLs without credentials.
    Unsupported fields, invalid values, and oversized posts raise ValueError.
    """
    return _prepare(post)[0]


def render_fallback(post: Mapping) -> str:
    """Validate the same data and return accessible plain text with file links."""
    return _prepare(post)[1]
