"""Deterministic extraction: what a fetched body actually says (#739).

Everything here is a pure function over bytes or text — no network, no model, no state —
so the interesting cases are unit-testable from a fixture file.  The orchestration that
decides *which* of these to call lives in :mod:`epicurus_websearch.ingest`.

Extraction is [trafilatura](https://trafilatura.readthedocs.io/).  It was picked over
``readability-lxml`` + ``beautifulsoup4`` because it is *one* dependency for both halves of
what the tool contract needs: readability-grade main-text extraction **and** the metadata
(title, author, publication date, site name, lead image) that comes from OpenGraph /
JSON-LD / ``<meta>`` — with readability we would have had to add a soup parser for the
metadata anyway.  It is Apache-2.0, pure Python over ``lxml``, and needs no OS packages,
which keeps the websearch image as it is.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import trafilatura
from trafilatura.metadata import extract_metadata

# Kinds the tool reports. ``page`` is the honest answer for something fetched and parsed but
# not recognisably an article — a landing page, a listing, a profile.
KIND_ARTICLE = "article"
KIND_IMAGE = "image"
KIND_VIDEO = "video"
KIND_PAGE = "page"
KIND_UNREACHABLE = "unreachable"

# Platforms whose links are media, not articles: an ``<article>`` extractor gets nothing
# useful from them, and their substance is in oEmbed / OpenGraph metadata instead. Matched on
# the registrable-ish host suffix so ``m.youtube.com`` and ``www.youtube.com`` both hit.
MEDIA_HOSTS: dict[str, str] = {
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "vimeo.com": "Vimeo",
    "instagram.com": "Instagram",
    "tiktok.com": "TikTok",
    "twitter.com": "X",
    "x.com": "X",
    "soundcloud.com": "SoundCloud",
    "dailymotion.com": "Dailymotion",
    "twitch.tv": "Twitch",
    "facebook.com": "Facebook",
    "fb.watch": "Facebook",
}

# oEmbed endpoints that answer without an app token. Instagram and Facebook are deliberately
# absent: Meta retired the token-free oEmbed endpoints in October 2020, and #739's honesty
# rule rules out authenticating — so those platforms fall back to OpenGraph off the public
# page, which is what a signed-out browser would see too.
OEMBED_ENDPOINTS: dict[str, str] = {
    "youtube.com": "https://www.youtube.com/oembed",
    "youtu.be": "https://www.youtube.com/oembed",
    "vimeo.com": "https://vimeo.com/api/oembed.json",
    "tiktok.com": "https://www.tiktok.com/oembed",
    "soundcloud.com": "https://soundcloud.com/oembed",
    "dailymotion.com": "https://www.dailymotion.com/services/oembed",
}

# Where a platform sends a signed-out visitor. Seeing one of these as the *final* URL means
# the content is login-walled, which the result says outright instead of reporting the login
# page's own title as the link's title.
_LOGIN_MARKERS = ("/login", "/accounts/login", "/signin", "/sign-in", "/auth/login")


@dataclass
class PageFacts:
    """What a fetched HTML page yields before any model is involved."""

    title: str = ""
    site: str = ""
    author: str | None = None
    published: str | None = None
    text: str = ""
    description: str = ""
    image: str = ""
    og_type: str = ""
    notes: list[str] = field(default_factory=list)


def host_of(url: str) -> str:
    """The lowercase hostname, without ``www.`` or a trailing dot."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def media_platform(url: str) -> str | None:
    """The media platform ``url`` belongs to, or ``None`` for an ordinary web page."""
    host = host_of(url)
    for suffix, name in MEDIA_HOSTS.items():
        if host == suffix or host.endswith("." + suffix):
            return name
    return None


def oembed_endpoint(url: str) -> str | None:
    """The token-free oEmbed endpoint for ``url``'s platform, if it has one."""
    host = host_of(url)
    for suffix, endpoint in OEMBED_ENDPOINTS.items():
        if host == suffix or host.endswith("." + suffix):
            return endpoint
    return None


def looks_login_walled(final_url: str, title: str = "") -> bool:
    """Whether the page we landed on is a sign-in screen rather than the content."""
    path = urlsplit(final_url).path.lower()
    if any(marker in path for marker in _LOGIN_MARKERS):
        return True
    lowered = title.lower()
    return bool(lowered) and ("log in" in lowered or "login •" in lowered)


def extract_page(html: str, *, url: str = "", max_chars: int = 20_000) -> PageFacts:
    """Pull title, byline, date, site, and main text out of an HTML document.

    Metadata and body are extracted separately on purpose: a video or profile page has no
    body worth extracting but does carry good OpenGraph metadata, and the caller needs the
    metadata either way.  Neither half raises — a malformed document yields empty fields and
    a note, because a partial answer plus an honest gap beats failing the whole ingest.
    """
    facts = PageFacts()
    try:
        # extensive=False turns off htmldate's most speculative pass. Explicit metadata
        # (OpenGraph, JSON-LD, <meta>) is unaffected; what goes away is the guess made from
        # any date-shaped string on the page, which on a content-free interstitial invents a
        # publication date out of nothing. A missing date beats a fabricated one.
        meta = extract_metadata(html, default_url=url or None, extensive=False)
    except Exception:  # trafilatura raises assorted parser errors on malformed input
        meta = None
    if meta is not None:
        facts.title = _clean(meta.title)
        facts.site = _clean(meta.sitename) or host_of(url)
        facts.author = plausible_author(_clean(meta.author))
        facts.published = plausible_date(_clean(meta.date))
        facts.description = _clean(meta.description)
        facts.image = _clean(getattr(meta, "image", "") or "")
    if not facts.site:
        facts.site = host_of(url)
    facts.og_type = _og_type(html)
    try:
        body = trafilatura.extract(
            html,
            url=url or None,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )
    except Exception:
        body = None
        facts.notes.append("the page's main text could not be parsed")
    if body:
        text = body.strip()
        if len(text) > max_chars:
            text = text[:max_chars].rstrip()
            facts.notes.append(
                f"the extract is the first {max_chars:,} characters of a longer page"
            )
        facts.text = text
    if not facts.title:
        facts.title = _title_tag(html)
    return facts


def parse_oembed(payload: bytes) -> dict[str, str]:
    """An oEmbed JSON response, flattened to the string fields we use.

    Returns ``{}`` for anything that isn't a JSON object — an endpoint that answers with an
    error page is a miss, not a crash.
    """
    try:
        data: Any = json.loads(payload.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    keep = ("title", "author_name", "provider_name", "thumbnail_url", "html", "type")
    return {key: str(data[key]) for key in keep if isinstance(data.get(key), str)}


_VTT_TIMING = re.compile(r"^\d{2}:\d{2}[:.][\d:.]+\s+-->\s+")
_VTT_TAG = re.compile(r"</?[cvbiu][^>]*>")
_VTT_NOISE = ("WEBVTT", "NOTE ", "STYLE", "REGION", "Kind:", "Language:")


def parse_vtt(payload: bytes, *, max_chars: int = 20_000) -> str:
    """WebVTT (or SRT) captions reduced to readable prose.

    Cue numbers, timings, positioning tags, and consecutive duplicate lines all go — the
    last of those matters because rolling captions repeat each line as the window scrolls,
    which would otherwise triple the text a model has to read.
    """
    lines: list[str] = []
    previous = ""
    for raw in payload.decode("utf-8", errors="replace").splitlines():
        line = _VTT_TAG.sub("", raw).strip()
        if not line or line.isdigit() or _VTT_TIMING.match(line):
            continue
        if line.startswith(_VTT_NOISE):
            continue
        if line == previous:
            continue
        lines.append(line)
        previous = line
    text = " ".join(lines).strip()
    return text[:max_chars].rstrip() if len(text) > max_chars else text


# A byline is a name (or a few, semicolon-joined), not a paragraph. Without a ceiling, a
# heuristic extractor happily reports a navigation block as the author — a real observed case
# was Wikipedia's "Authority control databases International GND National Japan". An absent
# author is honest; an invented one gets copied into whatever the agent files.
_MAX_AUTHOR_CHARS = 80
_MAX_AUTHOR_WORDS = 6
# Content older than the web, or dated in the future, is a parse artifact rather than a fact.
_MIN_YEAR = 1995
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def plausible_author(author: str) -> str | None:
    """``author`` if it reads like a byline, else ``None``.

    Applied per name so a genuine multi-author credit (``"A; B"``) survives while a single
    over-long run of nav text does not.
    """
    if not author or len(author) > _MAX_AUTHOR_CHARS:
        return None
    names = [part.strip() for part in author.split(";") if part.strip()]
    if not names:
        return None
    if any(len(name.split()) > _MAX_AUTHOR_WORDS for name in names):
        return None
    return author


def plausible_date(date: str) -> str | None:
    """``date`` if it is an ISO ``YYYY-MM-DD`` in a believable window, else ``None``."""
    match = _ISO_DATE_RE.match(date)
    if match is None:
        return date or None  # a non-ISO string is the source's own wording; pass it through
    year = int(match.group(1))
    return date if _MIN_YEAR <= year <= datetime.now(UTC).year + 1 else None


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_OG_TYPE_RE = re.compile(
    r"""<meta[^>]+property=["']og:type["'][^>]+content=["']([^"']+)["']""", re.IGNORECASE
)


def _title_tag(html: str) -> str:
    match = _TITLE_RE.search(html)
    return _clean(match.group(1)) if match else ""


def _og_type(html: str) -> str:
    match = _OG_TYPE_RE.search(html)
    return match.group(1).strip().lower() if match else ""


def _clean(value: str | None) -> str:
    """Collapse whitespace; ``None`` becomes an empty string."""
    return " ".join(value.split()) if value else ""
