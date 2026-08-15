"""``link_ingest`` orchestration: from a URL to the substance behind it (#739).

Three tiers, chosen from the link's host and the response's content type:

1. **HTML / articles** — guarded fetch, then readability-grade extraction plus
   OpenGraph/JSON-LD metadata (:mod:`epicurus_websearch.extract`).
2. **Images** — guarded fetch of the bytes, then a description from the **core's** vision
   model through the platform API (:mod:`epicurus_websearch.vision`).  The module holds no
   model access (constraint #8); when the core refuses because the resolved model has no
   vision, the result degrades to metadata plus a note saying exactly that.
3. **Video / reels / audio** — metadata and the *uploader's own* subtitles, from oEmbed,
   OpenGraph, and (for allow-listed public platforms) yt-dlp
   (:mod:`epicurus_websearch.media`).  Nothing is downloaded and nothing is transcribed.

Two rules run through all of it:

* **The module never calls another module** (ADR-0004).  ``link_ingest`` returns the
  extract; deciding what it means and filing it into the knowledge base is the *agent's*
  job, through the knowledge tools, in its own loop.
* **A failure is a result, not an exception.**  A login wall, a dead host, a refused
  private address, a captionless video, an image no model could see — each comes back as a
  well-formed result carrying an honest note.  The agent can then tell the operator what it
  could and could not get, which is the whole point of #739's honesty rule; a raised
  exception would just end the turn.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, Field

from epicurus_core import get_logger
from epicurus_websearch import media
from epicurus_websearch.extract import (
    KIND_ARTICLE,
    KIND_IMAGE,
    KIND_PAGE,
    KIND_UNREACHABLE,
    KIND_VIDEO,
    PageFacts,
    extract_page,
    host_of,
    looks_login_walled,
    media_platform,
    oembed_endpoint,
    parse_oembed,
    parse_vtt,
)
from epicurus_websearch.safety import (
    Fetched,
    FetchFailed,
    GuardedFetcher,
    UrlRefused,
)
from epicurus_websearch.vision import VisionCaptioner

log = get_logger("epicurus_websearch.ingest")

# Below this, an "article" is really a stub, a paywall teaser, or a navigation page — call it
# a page and let the agent judge, rather than presenting two sentences as the article.
_ARTICLE_MIN_CHARS = 400

# Content types a subtitle track comes back as. Platforms are inconsistent here (YouTube's
# timedtext answers ``text/xml`` or an octet-stream depending on the format), so this is
# deliberately wider than the page allow-list; the byte cap and the SSRF guard still apply.
_SUBTITLE_TYPES = ("text/", "application/json", "application/xml", "application/octet-stream")

_PAGE_ACCEPT = "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.8,*/*;q=0.5"
_IMAGE_ACCEPT = "image/*"


class IngestResult(BaseModel):
    """What ``link_ingest`` learned about one link.

    ``transcript`` is **reserved and always empty** in this version: automatic speech
    recognition is a new gateway modality (#739 defers it to milestone 5.0.0), and the field
    exists now so ASR can land later without changing the tool contract.  Uploader-provided
    subtitles are *not* a transcript — they arrive as part of ``text``, labelled as captions,
    so nothing here ever passes machine-heard speech off as the source's own words.
    """

    kind: str
    url: str
    title: str = ""
    site: str = ""
    author: str | None = None
    published: str | None = None
    text: str = ""
    image_descriptions: list[str] = Field(default_factory=list)
    transcript: str = ""
    notes: list[str] = Field(default_factory=list)
    retrieved_at: str = ""
    ok: bool = True

    @property
    def summary(self) -> str:
        """A one-line description for the entity-ref chip."""
        if self.text:
            first = self.text.strip().split("\n", 1)[0]
            return first[:200]
        return self.notes[0][:200] if self.notes else ""


class LinkIngestor:
    """Turns a URL into an :class:`IngestResult`. Stateless apart from its HTTP client."""

    def __init__(
        self,
        *,
        fetcher: GuardedFetcher,
        captioner: VisionCaptioner,
        max_text_chars: int = 20_000,
        use_media_probe: bool = True,
    ) -> None:
        self._fetcher = fetcher
        self._captioner = captioner
        self._max_text_chars = max_text_chars
        self._use_media_probe = use_media_probe

    async def ingest(self, url: str) -> IngestResult:
        """Read ``url`` and return what is behind it. Never raises for a bad link."""
        normalized = _normalize(url)
        if not normalized:
            return _unreachable(url, "that does not look like a link")
        platform = media_platform(normalized)
        try:
            if platform is not None:
                return await self._ingest_media(normalized, platform)
            return await self._ingest_web(normalized)
        except UrlRefused as exc:
            return _unreachable(normalized, exc.reason, refused=True)
        except FetchFailed as exc:
            return _unreachable(normalized, exc.reason)

    # ── tier 1 & 2: an ordinary web link ───────────────────────────────────────────────

    async def _ingest_web(self, url: str) -> IngestResult:
        fetched = await self._fetcher.fetch(url, accept=_PAGE_ACCEPT)
        if fetched.content_type.startswith("image/"):
            return await self._ingest_image(fetched)
        if fetched.content_type.startswith(("text/html", "application/xhtml")):
            return self._ingest_html(fetched)
        return self._ingest_plain(fetched)

    def _ingest_html(self, fetched: Fetched) -> IngestResult:
        facts = extract_page(fetched.text(), url=fetched.url, max_chars=self._max_text_chars)
        result = _from_facts(fetched.url, facts)
        if looks_login_walled(fetched.url, facts.title):
            result.kind = KIND_UNREACHABLE
            result.ok = False
            result.text = ""
            result.notes.insert(
                0,
                "the link is behind a login wall — this assistant never signs in, so only"
                " what a signed-out visitor can see was read (which here was nothing)",
            )
            return result
        result.kind = _classify_page(facts)
        if fetched.truncated:
            result.notes.append("the page was larger than the fetch cap and was read only in part")
        if not result.text and facts.description:
            result.text = facts.description
            result.notes.append(
                "the page had no extractable body text — this is its own summary/description"
            )
        if not result.text:
            result.notes.append("no readable body text could be extracted from this page")
        return result

    def _ingest_plain(self, fetched: Fetched) -> IngestResult:
        text = fetched.text(self._max_text_chars).strip()
        result = IngestResult(
            kind=KIND_PAGE,
            url=fetched.url,
            title=_name_from_url(fetched.url),
            site=host_of(fetched.url),
            text=text,
            retrieved_at=_now(),
        )
        result.notes.append(f"read as plain {fetched.content_type or 'text'}, not a web page")
        if fetched.truncated:
            result.notes.append("the file was larger than the fetch cap and was read only in part")
        return result

    async def _ingest_image(self, fetched: Fetched) -> IngestResult:
        result = IngestResult(
            kind=KIND_IMAGE,
            url=fetched.url,
            title=_name_from_url(fetched.url),
            site=host_of(fetched.url),
            retrieved_at=_now(),
        )
        if fetched.truncated:
            # Half a JPEG is not an image: sending it would either error at the provider or
            # produce a description of nothing. Say what happened instead.
            result.notes.append(
                "the image was larger than the fetch cap, so it could not be described"
            )
            return result
        caption = await self._captioner.caption(
            mime=fetched.content_type or "image/jpeg",
            data_b64=base64.b64encode(fetched.body).decode("ascii"),
        )
        if caption.text:
            result.image_descriptions.append(caption.text)
            result.text = caption.text
        if caption.note:
            result.notes.append(caption.note)
        return result

    # ── tier 3: a video / reel / audio link ────────────────────────────────────────────

    async def _ingest_media(self, url: str, platform: str) -> IngestResult:
        result = IngestResult(kind=KIND_VIDEO, url=url, site=platform, retrieved_at=_now())
        facts, page_note = await self._page_facts(url)
        if page_note:
            result.notes.append(page_note)
        oembed = await self._oembed(url)
        probe = await self._probe(url)

        result.title = _first(probe.title if probe else "", oembed.get("title", ""), facts.title)
        result.author = (
            _first(probe.uploader if probe else "", oembed.get("author_name", ""), facts.author)
            or None
        )
        result.published = _first(probe.published if probe else "", facts.published) or None
        body = _first(probe.description if probe else "", facts.description, facts.text)
        if body:
            result.text = body[: self._max_text_chars]
        if probe is not None:
            result.notes.extend(probe.notes)
            if probe.duration_s:
                result.notes.append(f"duration: {_hms(probe.duration_s)}")

        await self._add_captions(result, probe)
        await self._describe_thumbnail(result, probe, facts, oembed)

        walled = looks_login_walled(url, result.title) or (
            page_note is not None and probe is None and not oembed and not result.title
        )
        if walled:
            result.kind = KIND_UNREACHABLE
            result.ok = False
            result.notes.insert(
                0,
                "this link is private or behind a login wall — this assistant never signs in"
                " or uses credentials, so nothing could be read from it",
            )
            return result
        if not result.text and not result.image_descriptions:
            result.kind = KIND_PAGE if not result.title else KIND_VIDEO
            result.notes.append(
                "only the link's basic metadata was available — no description, captions,"
                " or image description could be retrieved"
            )
        return result

    async def _page_facts(self, url: str) -> tuple[PageFacts, str | None]:
        """OpenGraph facts off the public page — the fallback when oEmbed/yt-dlp give nothing."""
        try:
            fetched = await self._fetcher.fetch(url, accept=_PAGE_ACCEPT)
        except (UrlRefused, FetchFailed) as exc:
            return PageFacts(), f"the page itself could not be read: {exc.reason}"
        if not fetched.content_type.startswith(("text/html", "application/xhtml")):
            return PageFacts(), None
        return extract_page(fetched.text(), url=fetched.url, max_chars=self._max_text_chars), None

    async def _oembed(self, url: str) -> dict[str, str]:
        """The platform's token-free oEmbed record, or ``{}``.

        Instagram and Facebook have no token-free endpoint since Meta retired theirs in 2020,
        so those links rely on OpenGraph off the public page — see
        :data:`~epicurus_websearch.extract.OEMBED_ENDPOINTS`.
        """
        endpoint = oembed_endpoint(url)
        if endpoint is None:
            return {}
        try:
            fetched = await self._fetcher.fetch(
                f"{endpoint}?url={quote(url, safe='')}&format=json",
                accept="application/json",
                allowed_types=("application/json", "text/"),
            )
        except (UrlRefused, FetchFailed) as exc:
            log.info("oembed lookup failed", url=url, reason=exc.reason)
            return {}
        return parse_oembed(fetched.body)

    async def _probe(self, url: str) -> media.MediaFacts | None:
        if not self._use_media_probe:
            return None
        return await media.probe(url, timeout_s=self._fetcher.limits.timeout_s)

    async def _add_captions(self, result: IngestResult, probe: media.MediaFacts | None) -> None:
        """Append the uploader's own subtitles to ``text``, or say why there are none.

        Captions land in ``text`` rather than ``transcript`` on purpose: ``transcript`` is
        reserved for real ASR (out of scope, #739), and labelling the uploader's subtitles as
        a transcript would blur a line the contract deliberately draws.
        """
        if probe is None:
            return
        if not probe.subtitle_url:
            if probe.has_only_auto_captions:
                result.notes.append(
                    "only the platform's machine-generated captions are available; they were"
                    " not used — automatic transcription is out of scope for now"
                )
            else:
                result.notes.append(
                    "the uploader published no subtitles, and speech is not transcribed yet,"
                    " so nothing that was said in the video is captured here"
                )
            return
        try:
            fetched = await self._fetcher.fetch(
                probe.subtitle_url, accept="*/*", allowed_types=_SUBTITLE_TYPES
            )
        except (UrlRefused, FetchFailed) as exc:
            result.notes.append(f"the uploader's subtitles could not be fetched: {exc.reason}")
            return
        captions = parse_vtt(fetched.body, max_chars=self._max_text_chars)
        if not captions:
            result.notes.append("the uploader's subtitle track was empty or unreadable")
            return
        label = f"Captions ({probe.subtitle_lang}, published by the uploader):"
        result.text = f"{result.text}\n\n{label}\n{captions}".strip()

    async def _describe_thumbnail(
        self,
        result: IngestResult,
        probe: media.MediaFacts | None,
        facts: PageFacts,
        oembed: dict[str, str],
    ) -> None:
        """Run the video's poster frame through tier 2, when there is one and vision is wired.

        Only ever the *thumbnail* of a media link — an article's lead image is usually a stock
        photo, and captioning one on every ingest would spend an inference call per link for
        almost no signal.
        """
        thumbnail = _first(
            probe.thumbnail if probe else "", oembed.get("thumbnail_url", ""), facts.image
        )
        if not thumbnail or not self._captioner.available:
            return
        try:
            fetched = await self._fetcher.fetch(
                thumbnail, accept=_IMAGE_ACCEPT, allowed_types=("image/",)
            )
        except (UrlRefused, FetchFailed) as exc:
            result.notes.append(f"the thumbnail could not be fetched: {exc.reason}")
            return
        if fetched.truncated:
            result.notes.append("the thumbnail was larger than the fetch cap and was not described")
            return
        caption = await self._captioner.caption(
            mime=fetched.content_type or "image/jpeg",
            data_b64=base64.b64encode(fetched.body).decode("ascii"),
        )
        if caption.text:
            result.image_descriptions.append(f"Thumbnail: {caption.text}")
        if caption.note:
            result.notes.append(caption.note)


# ── rendering ──────────────────────────────────────────────────────────────────────────


def render(result: IngestResult) -> str:
    """The result as the block of text the model reads back from the tool.

    Deliberately a labelled document rather than raw JSON: the agent's next move is to write
    prose about it (and often to file it), and a heading-plus-fields layout survives being
    quoted into a knowledge document far better than a serialized object would.
    """
    lines: list[str] = []
    header = result.title or result.url
    lines.append(f"# {header}")
    facts = [f"kind: {result.kind}"]
    if result.site:
        facts.append(f"site: {result.site}")
    if result.author:
        facts.append(f"author: {result.author}")
    if result.published:
        facts.append(f"published: {result.published}")
    if result.retrieved_at:
        facts.append(f"retrieved: {result.retrieved_at}")
    lines.append(" · ".join(facts))
    lines.append(f"source: {result.url}")
    if result.text:
        lines.append("")
        lines.append(result.text)
    if result.image_descriptions:
        lines.append("")
        lines.append("Image descriptions:")
        lines.extend(f"- {description}" for description in result.image_descriptions)
    if result.notes:
        lines.append("")
        lines.append("Notes (tell the operator what was and was not retrieved):")
        lines.extend(f"- {note}" for note in result.notes)
    return "\n".join(lines)


# ── helpers ────────────────────────────────────────────────────────────────────────────


def _normalize(url: str) -> str:
    """Trim, and add ``https://`` to a bare ``example.com/path``. Empty when unusable."""
    candidate = url.strip().strip("<>").rstrip(".,)")
    if not candidate:
        return ""
    if "://" not in candidate:
        if candidate.startswith("//"):
            candidate = f"https:{candidate}"
        elif "." in candidate.split("/", 1)[0]:
            candidate = f"https://{candidate}"
        else:
            return ""
    return candidate


def _classify_page(facts: PageFacts) -> str:
    if facts.og_type.startswith("video"):
        return KIND_VIDEO
    if facts.og_type == "article" or len(facts.text) >= _ARTICLE_MIN_CHARS:
        return KIND_ARTICLE
    return KIND_PAGE


def _from_facts(url: str, facts: PageFacts) -> IngestResult:
    return IngestResult(
        kind=KIND_PAGE,
        url=url,
        title=facts.title,
        site=facts.site or host_of(url),
        author=facts.author,
        published=facts.published,
        text=facts.text,
        notes=list(facts.notes),
        retrieved_at=_now(),
    )


def _unreachable(url: str, reason: str, *, refused: bool = False) -> IngestResult:
    lead = "refused" if refused else "could not be read"
    return IngestResult(
        kind=KIND_UNREACHABLE,
        url=url,
        site=host_of(url),
        ok=False,
        notes=[f"{lead}: {reason}"],
        retrieved_at=_now(),
    )


def _name_from_url(url: str) -> str:
    """The last path segment, as a stand-in title for a file that has none."""
    return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or host_of(url)


def _first(*candidates: str | None) -> str:
    for candidate in candidates:
        if candidate:
            return candidate
    return ""


def _hms(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _now() -> str:
    """The retrieval date, UTC — the agent embeds it in anything it files (#739)."""
    return datetime.now(UTC).date().isoformat()
