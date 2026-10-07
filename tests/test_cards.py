"""Behavioral tests for the user-visible channel card contract."""

from copy import deepcopy
import json

import pytest

from cards import MAX_MESSAGE_BYTES, build_card, render_fallback


def sample_post(template="report"):
    return {
        "template": template,
        "title": "October delivery update",
        "summary": "The files are ready for review.",
        "status": "Ready",
        "facts": [{"label": "Owner", "value": "Alex"}, {"label": "Files", "value": "3"}],
        "sections": [{"title": "Next step", "text": "Review the report and share your feedback."}],
        "actions": [{"label": "Open report", "url": "https://example.sharepoint.com/sites/team/Report.docx"}],
    }


def elements(card):
    """Traverse the visible card body, including composed section containers."""
    for element in card.get("body", card.get("items", [])):
        yield element
        yield from elements(element)


@pytest.mark.parametrize("template,eyebrow", [
    ("report", "REPORT"), ("status", "STATUS UPDATE"), ("document", "DOCUMENT"),
])
def test_templates_preserve_readable_content_and_file_action(template, eyebrow):
    post = sample_post(template)
    original = deepcopy(post)
    card = build_card(post)

    assert post == original
    assert card["type"] == "AdaptiveCard"
    assert card["version"] == "1.4"
    visible = list(elements(card))
    text = [item for item in visible if item["type"] == "TextBlock"]
    assert text[0]["text"] == eyebrow
    assert text[1]["text"] == post["title"]
    assert next(item for item in text if item["text"] == "Status: Ready")["color"] == "Good"
    assert card["actions"] == [{"type": "Action.OpenUrl", "title": "Open report", "url": post["actions"][0]["url"]}]
    assert all(item.get("wrap") is True for item in text)
    assert {item["type"] for item in visible} <= {"TextBlock", "FactSet", "Container"}
    fallback = render_fallback(post)
    assert card["fallbackText"] == fallback
    for text in [post["title"], post["summary"], "Status: Ready", "Owner: Alex",
                 post["sections"][0]["text"], post["actions"][0]["url"]]:
        assert text in fallback


def test_minimal_post_omits_empty_controls_and_allows_plain_unknown_status():
    card = build_card({"template": "status", "title": "Pipeline", "status": "Awaiting supplier"})
    assert "actions" not in card
    assert len(card["body"]) == 2
    assert card["body"][1]["style"] == "emphasis"
    assert card["body"][1]["items"][0]["color"] == "Default"
    assert card["fallbackText"] == "Pipeline\n\nStatus: Awaiting supplier"


def test_user_prose_cannot_inject_card_markdown_links_or_emphasis():
    post = {"template": "document", "title": "[Open](https://evil.example) **bold**",
            "facts": [{"label": "_Owner_", "value": "[here](https://evil.example)"}]}
    card = build_card(post)
    visible = list(elements(card))
    assert visible[2]["text"] == r"\[Open\]\(https://evil\.example\) \*\*bold\*\*"
    assert next(item for item in visible if item["type"] == "FactSet")["facts"][0]["title"] == r"\_Owner\_"
    assert render_fallback(post).startswith(post["title"])
    assert "actions" not in card


@pytest.mark.parametrize("url", [
    "http://example.com/file", "javascript:alert(1)", "file:///C:/secret.txt",
    "https://user:password@example.com/file", "https://user@example.com/file",
    "https://localhost/file", "https://host.local/file", "https://host.internal/file",
    "https://127.0.0.1/file", "https://10.0.0.8/file", "https://169.254.169.254/file",
    "https://[::1]/file", "https://127.1/file", "https://2130706433/file",
    "https://0177.0.0.1/file", "https://0x7f.0.0.1/file", "https://127.0.0.0x1/file",
    "https://example.com:8080/file", "https://example.com\\@evil.example/file",
    "https://example.com/file?access_token=secret", "https://example.com/file?sig=secret",
    "https://example.com/file#id_token=secret", "https://example.com/file?X-Amz-Signature=secret",
    "https://example.com/file?%61pi_key=secret", "https://example.com/file%0D%0Ainjected",
    "https://bad_host.example/file", "https://example.com/a b",
])
def test_rejects_unsafe_action_urls(url):
    post = {"template": "document", "title": "File", "actions": [{"label": "Open", "url": url}]}
    with pytest.raises(ValueError):
        build_card(post)
    with pytest.raises(ValueError):
        render_fallback(post)


@pytest.mark.parametrize("url", [
    "https://example.sharepoint.com/sites/team/My%20File.docx?web=1",
    "https://example.com:443/report#page=2", "https://example.com/report?year=2026",
])
def test_allows_durable_https_document_urls(url):
    card = build_card({"template": "document", "title": "File",
                       "actions": [{"label": "Open file", "url": url}]})
    assert card["actions"][0]["url"] == url


@pytest.mark.parametrize("post", [
    None, [], {}, {"template": "report"}, {"template": "custom", "title": "Title"},
    {"template": "report", "title": " "}, {"template": "report", "title": "a\nb"},
    {"template": "report", "title": "a\x00b"}, {"template": "report", "title": "\ud800"},
    {"template": "report", "title": "Title", "summary": {"html": "content"}},
    {"template": "report", "title": "Title", "metadata": {"token": "secret"}},
    {"template": "report", "title": "Title", "facts": {"label": "Owner", "value": "A"}},
    {"template": "report", "title": "Title", "facts": [{"label": "Count", "value": 5}]},
    {"template": "report", "title": "Title", "sections": [{"title": "Empty"}]},
    {"template": "report", "title": "Title", "actions": [{"label": "Run", "type": "Action.Execute"}]},
])
def test_invalid_structures_have_consistent_errors(post):
    for renderer in (build_card, render_fallback):
        with pytest.raises(ValueError):
            renderer(post)


@pytest.mark.parametrize("field,value", [
    ("title", "x" * 201), ("summary", "x" * 2001), ("status", "x" * 81),
    ("facts", [{"label": "Name", "value": "Value"}] * 13),
    ("sections", [{"text": "Content"}] * 7),
    ("actions", [{"label": "Open", "url": "https://example.com"}] * 4),
])
def test_per_field_limits(field, value):
    post = {"template": "report", "title": "Title", field: value}
    with pytest.raises(ValueError):
        build_card(post)


@pytest.mark.parametrize("template", ["report", "status", "document"])
def test_total_limit_counts_utf8_and_repeated_accessible_fallback(template):
    post = {"template": template, "title": "Report",
            "sections": [{"text": "漢" * 2000}, {"text": "字" * 2000}]}
    # Every individual field is valid and the character count is small, but
    # UTF-8 plus the repeated accessibility content exceeds transport limits.
    with pytest.raises(ValueError, match="28 KiB"):
        build_card(post)
    with pytest.raises(ValueError, match="28 KiB"):
        render_fallback(post)


@pytest.mark.parametrize("template", ["report", "status", "document"])
def test_valid_message_is_bounded_when_wrapped_for_transport(template):
    post = sample_post(template)
    envelope = {"type": "message", "summary": render_fallback(post), "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "content": build_card(post),
    }]}
    assert len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) < MAX_MESSAGE_BYTES


def test_multiline_plain_text_is_preserved_in_fallback():
    post = {"template": "report", "title": "Report", "summary": "First\r\nSecond",
            "sections": [{"text": "Third\nFourth"}]}
    assert render_fallback(post) == "Report\n\nFirst\nSecond\n\nThird\nFourth"


def test_layouts_prioritize_different_content_without_changing_fallback():
    report = build_card(sample_post("report"))
    status = build_card(sample_post("status"))
    document = build_card(sample_post("document"))

    assert report["body"][0]["style"] == "accent"
    assert report["body"][1]["type"] == "TextBlock"  # Report summary stands alone.
    assert report["body"][2]["style"] == "emphasis"
    assert report["body"][2]["items"][0]["text"] == "At a glance"
    assert status["body"][0]["style"] == "default"
    assert status["body"][1]["style"] == "good"
    assert [item["text"] for item in status["body"][1]["items"]] == [
        "Status: Ready", r"The files are ready for review\.",
    ]
    assert document["body"][0]["style"] == "emphasis"
    assert document["body"][0]["items"][-1]["text"] == r"The files are ready for review\."
    assert document["body"][1]["items"][0]["text"] == "File details"
    assert report["fallbackText"] == status["fallbackText"] == document["fallbackText"]
    assert report["actions"] == status["actions"] == document["actions"]


@pytest.mark.parametrize("status,style", [
    ("Ready", "good"), ("In progress", "warning"), ("Blocked", "attention"),
    ("Awaiting supplier", "emphasis"),
])
def test_status_callout_keeps_meaning_visible_without_color(status, style):
    card = build_card({"template": "status", "title": "Delivery", "status": status})
    panel = card["body"][1]
    assert panel["style"] == style
    assert panel["items"][0]["text"] == f"Status: {status}"
    assert f"Status: {status}" in card["fallbackText"]


@pytest.mark.parametrize("template", ["report", "status", "document"])
def test_minimal_layouts_have_no_empty_panels_or_invented_file_actions(template):
    card = build_card({"template": template, "title": "Only a title"})
    assert len(card["body"]) == 1
    assert "actions" not in card
    assert card["fallbackText"] == "Only a title"
    assert all(item["items"] for item in elements(card) if item["type"] == "Container")


@pytest.mark.parametrize("template", ["report", "status", "document"])
def test_all_sections_remain_readable_groups_with_optional_titles(template):
    post = {"template": template, "title": "Review", "sections": [
        {"title": "Next step", "text": "Read the findings"},
        {"text": "Keep this untitled note"},
    ]}
    card = build_card(post)
    titled, untitled = card["body"][-2:]
    assert titled["separator"] is True and untitled["separator"] is True
    assert [item["text"] for item in titled["items"]] == ["Next step", "Read the findings"]
    assert [item["text"] for item in untitled["items"]] == ["Keep this untitled note"]
    assert all(item["wrap"] for item in elements(card) if item["type"] == "TextBlock")
    assert all("maxLines" not in item and "minHeight" not in item for item in elements(card))
    assert "msteams" not in card  # No forced wide layout or hidden mention entities.


def test_status_summary_without_status_does_not_invent_state():
    card = build_card({"template": "status", "title": "Review", "summary": "Comments requested"})
    assert card["body"][1]["style"] == "emphasis"
    assert card["body"][1]["items"] == [{
        "type": "TextBlock", "text": "Comments requested", "wrap": True, "spacing": "Default",
    }]
    assert card["fallbackText"] == "Review\n\nComments requested"


def test_report_size_guard_counts_composed_layout_overhead():
    # Find the exact accepted/rejected boundary with ASCII fields under each
    # individual limit, then compare against the actual transport envelope.
    post = {"template": "report", "title": "Report", "sections": [
        {"title": f"Section {i}", "text": "a" * 1000} for i in range(6)
    ]}
    lower, upper = 1000, 2000
    while lower + 1 < upper:
        middle = (lower + upper) // 2
        for section in post["sections"]:
            section["text"] = "a" * middle
        try:
            build_card(post)
        except ValueError:
            upper = middle
        else:
            lower = middle
    for section in post["sections"]:
        section["text"] = "a" * lower
    card = build_card(post)
    envelope = {"type": "message", "summary": render_fallback(post), "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "content": card,
    }]}
    assert len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode()) <= MAX_MESSAGE_BYTES
    for section in post["sections"]:
        section["text"] = "a" * upper
    with pytest.raises(ValueError, match="28 KiB"):
        build_card(post)
