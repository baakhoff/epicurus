"""Document text extraction for attachments (#738) — a seam keyed by content type.

``attachments.py``'s expander used to decode every non-image file as UTF-8 text
(``errors="replace"``). For a PDF that produces thousands of replacement characters, not
content — the same failure mode #633 already named for images and special-cased away.
This module is where a format gets a real reader instead: :func:`extract_pdf` today, a
docx/pptx extractor slotting in the same way later. Every extractor returns the same
:class:`ExtractedDocument` shape so the expander's rendering logic stays format-agnostic.

An extractor never raises for "this file doesn't have real text" — encrypted, scanned
(image-only), and malformed all come back as ``has_text=False``. The caller renders an
honest metadata block for those cases (name, type, an honest reason) rather than mojibake
or a silently-dropped attachment; see ``attachments.py``'s ``_render_pdf``.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from pypdf import PdfReader

from epicurus_core import get_logger

log = get_logger("epicurus_core_app.agent.document_extraction")

# Larger than the plain-text excerpt cap (_EXCERPT_CHARS in attachments.py): a document
# attachment is *the point* of the turn, not incidental context, so it earns a bigger
# budget — but still hard-capped so a 500-page manual can't blow out the prompt.
PDF_EXCERPT_CHARS = 20_000

# A decode is "mostly binary" once replacement characters (U+FFFD) cross this share of the
# output — calibrated well above the noise floor of a genuinely-text file with a handful of
# non-UTF-8 bytes (~0.5% observed) and well below real binary content (~40%+ observed for
# compressed/image bytes decoded as UTF-8).
BINARY_REPLACEMENT_RATIO = 0.05

_REPLACEMENT_CHAR = "�"


@dataclass(frozen=True)
class ExtractedDocument:
    """What a format extractor hands back.

    ``has_text=False`` covers every "nothing to read" case alike — encrypted, scanned,
    malformed, empty — since the caller's honest-fallback rendering doesn't need to
    distinguish them. ``page_count`` is still reported when known (0 if the file couldn't
    even be parsed enough to count pages) so that fallback block can say *something*
    concrete.
    """

    text: str
    page_count: int
    has_text: bool
    truncated: bool


def extract_pdf(data: bytes, *, limit: int = PDF_EXCERPT_CHARS) -> ExtractedDocument:
    """Extract *data*'s text, page by page, each prefixed ``[page N]``, bounded to *limit*.

    Encrypted (beyond an empty/owner-only password — the common "restricts editing, not
    reading" case) and scanned (image-only, no text layer) PDFs both return
    ``has_text=False``, indistinguishable from each other and from a malformed file to the
    caller — all three are "nothing to extract," rendered as one honest fallback block.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # not a parseable PDF at all — never surfaces to the caller as
        # an error; extraction simply found nothing (see the module docstring)
        log.debug("pdf parse failed", error=str(exc))
        return ExtractedDocument(text="", page_count=0, has_text=False, truncated=False)

    if reader.is_encrypted:
        try:
            # PasswordType.NOT_DECRYPTED (0) means even an empty password failed — a real
            # user password protects this file, so its content is genuinely inaccessible.
            # Any other outcome (owner-only restriction, no real open-password) decrypts
            # with "" and reads normally.
            if reader.decrypt("") == 0:
                return ExtractedDocument(
                    text="", page_count=_safe_page_count(reader), has_text=False, truncated=False
                )
        except Exception as exc:
            log.debug("pdf decrypt failed", error=str(exc))
            return ExtractedDocument(text="", page_count=0, has_text=False, truncated=False)

    try:
        pages = list(reader.pages)
    except Exception as exc:  # a corrupt page tree — same "nothing to extract" outcome
        log.debug("pdf page tree unreadable", error=str(exc))
        return ExtractedDocument(text="", page_count=0, has_text=False, truncated=False)

    rendered: list[str] = []
    for i, page in enumerate(pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception as exc:  # one bad page must not lose the rest already extracted
            log.debug("pdf page extraction failed", page=i, error=str(exc))
            continue
        if text:
            rendered.append(f"[page {i}]\n{text}")

    joined = "\n\n".join(rendered)
    truncated = len(joined) > limit
    if truncated:
        joined = joined[:limit].rstrip() + "\n…(truncated)"
    return ExtractedDocument(
        text=joined, page_count=len(pages), has_text=bool(rendered), truncated=truncated
    )


def _safe_page_count(reader: PdfReader) -> int:
    """``len(reader.pages)`` can itself raise once decryption has failed — 0 rather than
    letting a page-count lookup take down the whole honest-fallback path."""
    try:
        return len(reader.pages)
    except Exception:
        return 0


def is_mostly_binary(text: str, *, threshold: float = BINARY_REPLACEMENT_RATIO) -> bool:
    """Whether a UTF-8 decode (``errors="replace"``) of some bytes produced mostly noise.

    The catch-all for any format with no dedicated extractor (a zip renamed ``.bin``, an
    unlabelled binary): if decoding it as text was mostly replacement characters, it was
    never text, and the caller should say so plainly instead of forwarding the noise.
    """
    if not text:
        return False
    return text.count(_REPLACEMENT_CHAR) / len(text) > threshold
