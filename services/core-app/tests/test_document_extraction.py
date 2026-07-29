"""Unit tests for document_extraction.py (#738): real PDF fixtures, not mocked pypdf calls.

Fixtures are built with ``pypdf.PdfWriter`` itself — a minimal real PDF with an actual
content stream and a standard (non-embedded) font, not a byte-for-byte hand-crafted file —
so these exercise the real parser, not an assumption about its behavior.
"""

from __future__ import annotations

import io

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from epicurus_core_app.agent.document_extraction import (
    BINARY_REPLACEMENT_RATIO,
    extract_pdf,
    is_mostly_binary,
)


def _text_pdf(pages_text: list[str]) -> bytes:
    """A real PDF whose pages actually contain the given text, extractable by pypdf."""
    writer = PdfWriter()
    for text in pages_text:
        page = writer.add_blank_page(width=612, height=792)
        content = DecodedStreamObject()
        content.set_data(f"BT /Helv 24 Tf 72 700 Td ({text}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(content)
        font = DictionaryObject()
        font[NameObject("/Type")] = NameObject("/Font")
        font[NameObject("/Subtype")] = NameObject("/Type1")
        font[NameObject("/BaseFont")] = NameObject("/Helvetica")
        resources = DictionaryObject()
        fonts = DictionaryObject()
        fonts[NameObject("/Helv")] = writer._add_object(font)
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _blank_pdf(n_pages: int) -> bytes:
    """A real, valid PDF with pages but no content stream at all — the scanned/image-only
    case as far as extract_text() is concerned (an empty string, never an error)."""
    writer = PdfWriter()
    for _ in range(n_pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _encrypted_pdf(pages_text: list[str], *, user_password: str) -> bytes:
    reader = PdfReader(io.BytesIO(_text_pdf(pages_text)))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password=user_password, owner_password=user_password)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ── extract_pdf ────────────────────────────────────────────────────────────────────


def test_extracts_real_text_with_page_markers() -> None:
    data = _text_pdf(["Hello World Page One", "Second Page Content Here"])
    doc = extract_pdf(data)
    assert doc.has_text is True
    assert doc.page_count == 2
    assert doc.truncated is False
    assert "[page 1]" in doc.text and "Hello World Page One" in doc.text
    assert "[page 2]" in doc.text and "Second Page Content Here" in doc.text
    # page 1's marker/text precedes page 2's in the joined output
    assert doc.text.index("[page 1]") < doc.text.index("[page 2]")


def test_no_u_fffd_in_extracted_text() -> None:
    """The mojibake regression this issue exists to fix (#738)."""
    data = _text_pdf(["Plain ASCII content, nothing exotic"])
    doc = extract_pdf(data)
    assert "�" not in doc.text


def test_scanned_pdf_reports_no_extractable_text() -> None:
    data = _blank_pdf(5)
    doc = extract_pdf(data)
    assert doc.has_text is False
    assert doc.text == ""
    assert doc.page_count == 5  # still a real, known page count


def test_encrypted_pdf_with_a_real_password_reports_no_extractable_text() -> None:
    data = _encrypted_pdf(["secret content"], user_password="pw123")
    doc = extract_pdf(data)
    assert doc.has_text is False
    assert doc.text == ""


def test_owner_only_encryption_still_extracts() -> None:
    """An empty user password (restricts editing, not opening) is the common case a strict
    "any encryption = no text" rule would wrongly refuse."""
    data = _encrypted_pdf(["visible content"], user_password="")
    doc = extract_pdf(data)
    assert doc.has_text is True
    assert "visible content" in doc.text


def test_malformed_bytes_report_no_extractable_text_not_an_exception() -> None:
    doc = extract_pdf(b"this is not a pdf at all, just random bytes")
    assert doc.has_text is False
    assert doc.text == ""
    assert doc.page_count == 0


def test_empty_bytes_report_no_extractable_text() -> None:
    doc = extract_pdf(b"")
    assert doc.has_text is False


def test_truncates_past_the_limit_with_a_note() -> None:
    long_page = "word " * 20  # keep the fixture-generation cheap; the limit below is tiny
    data = _text_pdf([long_page])
    doc = extract_pdf(data, limit=20)
    assert doc.truncated is True
    assert doc.text.endswith("…(truncated)")
    assert len(doc.text) <= 20 + len("\n…(truncated)")


def test_short_document_is_not_truncated() -> None:
    data = _text_pdf(["short"])
    doc = extract_pdf(data, limit=20_000)
    assert doc.truncated is False
    assert not doc.text.endswith("…(truncated)")


# ── is_mostly_binary ─────────────────────────────────────────────────────────────


def test_mostly_text_with_a_little_noise_is_not_binary() -> None:
    mostly_text = ("Hello world. " * 50) + "���"
    assert is_mostly_binary(mostly_text) is False


def test_heavily_replaced_text_is_binary() -> None:
    noisy = "�" * 500 + "a few real chars"
    assert is_mostly_binary(noisy) is True


def test_empty_string_is_not_binary() -> None:
    assert is_mostly_binary("") is False


def test_no_replacement_characters_is_not_binary() -> None:
    assert is_mostly_binary("perfectly ordinary text") is False


def test_threshold_is_a_share_not_a_count() -> None:
    # Just under vs. just over BINARY_REPLACEMENT_RATIO, holding length constant.
    n = 100
    under = int(n * BINARY_REPLACEMENT_RATIO) - 1
    over = int(n * BINARY_REPLACEMENT_RATIO) + 1
    assert is_mostly_binary("�" * under + "a" * (n - under)) is False
    assert is_mostly_binary("�" * over + "a" * (n - over)) is True
