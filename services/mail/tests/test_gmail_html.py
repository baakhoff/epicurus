"""Unit tests for HTML-body + inline-attachment extraction (ADR-0097, #627).

The provider now surfaces the raw ``text/html`` body and marks inline images (a ``Content-ID``
part an HTML body references as ``cid:<id>``) so the shell can render rich mail in a sandboxed
iframe. These pin the parsing; the *rendering* safety is tested web-side (`MailHtmlBody`).
"""

from __future__ import annotations

import base64
from typing import Any

from epicurus_mail.gmail import _extract_attachments, _extract_html, _parse_message


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


_HTML = '<p>Hi <img src="cid:logo"> <img src="https://tracker.example/x.gif"></p>'


def _html_with_inline_image() -> dict[str, Any]:
    """A multipart/related message: a text+html alternative plus an inline image part."""
    return {
        "id": "m-html",
        "threadId": "t1",
        "snippet": "Hi",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/related",
            "headers": [
                {"name": "Subject", "value": "Newsletter"},
                {"name": "From", "value": "news@example.com"},
            ],
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": _b64("Hi (plain)")}},
                        {"mimeType": "text/html", "body": {"data": _b64(_HTML)}},
                    ],
                },
                {
                    "mimeType": "image/png",
                    "filename": "logo.png",
                    "headers": [
                        {"name": "Content-ID", "value": "<logo>"},
                        {"name": "Content-Disposition", "value": "inline"},
                    ],
                    "body": {"attachmentId": "att-logo", "size": 1234},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "invoice.pdf",
                    "headers": [{"name": "Content-Disposition", "value": "attachment"}],
                    "body": {"attachmentId": "att-pdf", "size": 5000},
                },
            ],
        },
    }


def test_extract_html_returns_raw_html() -> None:
    payload = _html_with_inline_image()["payload"]
    html = _extract_html(payload)
    assert html is not None
    assert "cid:logo" in html  # returned verbatim, not decoded to text


def test_extract_html_none_for_text_only() -> None:
    payload = {"mimeType": "text/plain", "body": {"data": _b64("just text")}}
    assert _extract_html(payload) is None


def test_inline_image_carries_content_id_and_is_inline() -> None:
    atts = _extract_attachments(_html_with_inline_image()["payload"])
    by_id = {a.id: a for a in atts}
    logo = by_id["att-logo"]
    assert logo.content_id == "logo"  # angle brackets stripped
    assert logo.inline is True
    # An ordinary attachment carries no Content-ID and is not inline.
    pdf = by_id["att-pdf"]
    assert pdf.content_id is None
    assert pdf.inline is False
    assert pdf.filename == "invoice.pdf"


def test_parse_message_populates_body_html_and_text_fallback() -> None:
    msg = _parse_message(_html_with_inline_image(), full=True)
    assert msg.body_html is not None and "cid:logo" in msg.body_html
    assert msg.body == "Hi (plain)"  # text fallback still present for text-only consumers
    assert any(a.content_id == "logo" and a.inline for a in msg.attachments)


def test_metadata_read_has_no_html_or_attachments() -> None:
    # A metadata (non-full) parse never carries the body/attachments (search rows stay light).
    msg = _parse_message(_html_with_inline_image(), full=False)
    assert msg.body_html is None
    assert msg.attachments == []
