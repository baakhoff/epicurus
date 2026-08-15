"""Tests for ``link_ingest`` orchestration (ingest.py, #739).

Everything runs against a real :class:`GuardedFetcher` over ``httpx.MockTransport`` with an
injected resolver, so the guard, the redirect walk, and the caps are all genuinely in the
path — only the network is fake.  The yt-dlp probe is monkeypatched; the captioner is a
stub, so no model is needed.

The recurring assertion is the honesty rule: whatever goes wrong, the tool returns a
well-formed result whose notes say what was and was not retrieved.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from epicurus_websearch import media
from epicurus_websearch.extract import (
    KIND_ARTICLE,
    KIND_IMAGE,
    KIND_PAGE,
    KIND_UNREACHABLE,
    KIND_VIDEO,
)
from epicurus_websearch.ingest import IngestResult, LinkIngestor, render
from epicurus_websearch.safety import FetchLimits, GuardedFetcher, UrlGuard
from epicurus_websearch.vision import Caption, VisionCaptioner

FIXTURES = Path(__file__).parent / "fixtures"
ARTICLE = (FIXTURES / "article.html").read_bytes()
VIDEO_PAGE = (FIXTURES / "video_page.html").read_bytes()
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PUBLIC = "93.184.216.34"


class _StubCaptioner(VisionCaptioner):
    """A captioner with a canned answer — the real one is covered in test_vision.py."""

    def __init__(self, *, caption: Caption | None = None, available: bool = True) -> None:
        super().__init__(None)
        self._canned = caption or Caption(text="A photograph of a harbour wall.")
        self._available = available
        self.calls: list[str] = []

    @property
    def available(self) -> bool:
        return self._available

    async def caption(self, *, mime: str, data_b64: str) -> Caption:
        self.calls.append(mime)
        return self._canned


def _resolver(*addresses: str) -> Any:
    answer: Sequence[str] = addresses or (PUBLIC,)

    async def resolve(_host: str) -> Sequence[str]:
        return answer

    return resolve


def _ingestor(
    handler: Any,
    *,
    captioner: VisionCaptioner | None = None,
    max_text_chars: int = 20_000,
    use_media_probe: bool = False,
    limits: FetchLimits | None = None,
) -> LinkIngestor:
    fetcher = GuardedFetcher(
        guard=UrlGuard(resolve=_resolver()),
        limits=limits or FetchLimits(timeout_s=5.0),
        transport=httpx.MockTransport(handler),
    )
    return LinkIngestor(
        fetcher=fetcher,
        captioner=captioner or _StubCaptioner(),
        max_text_chars=max_text_chars,
        use_media_probe=use_media_probe,
    )


def _serve(routes: dict[str, tuple[int, bytes, str]] | None = None) -> Any:
    """A handler mapping request paths to ``(status, body, content-type)``; 404 otherwise."""
    table = routes or {}

    def handler(request: httpx.Request) -> httpx.Response:
        entry = table.get(request.url.path)
        if entry is None:
            return httpx.Response(404)
        status, body, content_type = entry
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    return handler


# ── tier 1: articles and pages ─────────────────────────────────────────────────────────


async def test_article_ingest_returns_metadata_and_body() -> None:
    ingestor = _ingestor(_serve({"/a/tidal": (200, ARTICLE, "text/html; charset=utf-8")}))
    result = await ingestor.ingest("https://coastalreview.example/a/tidal")
    assert result.ok is True
    assert result.kind == KIND_ARTICLE
    assert result.title == "Tidal turbines feed a village for a winter"
    assert result.site == "The Coastal Review"
    assert result.author == "Marit Halvorsen"
    assert result.published == "2025-11-14"
    assert "Kilbrannan Sound" in result.text
    assert result.retrieved_at  # the date the agent embeds in what it files


async def test_transcript_is_reserved_and_always_empty() -> None:
    """ASR is out of scope (#739); the field exists only so it can land without a rewrite."""
    ingestor = _ingestor(_serve({"/a/tidal": (200, ARTICLE, "text/html")}))
    result = await ingestor.ingest("https://coastalreview.example/a/tidal")
    assert result.transcript == ""


async def test_a_short_page_is_reported_as_a_page_not_an_article() -> None:
    stub = b"<html><head><title>Hi</title></head><body><p>Two words.</p></body></html>"
    result = await _ingestor(_serve({"/p": (200, stub, "text/html")})).ingest(
        "https://example.com/p"
    )
    assert result.kind == KIND_PAGE


async def test_a_page_without_body_text_falls_back_to_its_own_description() -> None:
    result = await _ingestor(_serve({"/w/abc123": (200, VIDEO_PAGE, "text/html")})).ingest(
        "https://cliptube.example/w/abc123"
    )
    assert "Eleven months" in result.text
    assert any("no extractable body text" in note for note in result.notes)


async def test_og_type_video_classifies_a_plain_host_as_video() -> None:
    result = await _ingestor(_serve({"/w/abc123": (200, VIDEO_PAGE, "text/html")})).ingest(
        "https://cliptube.example/w/abc123"
    )
    assert result.kind == KIND_VIDEO


async def test_a_truncated_page_says_so() -> None:
    ingestor = _ingestor(
        _serve({"/a/tidal": (200, ARTICLE, "text/html")}),
        limits=FetchLimits(max_bytes=400, timeout_s=5.0),
    )
    result = await ingestor.ingest("https://coastalreview.example/a/tidal")
    assert any("read only in part" in note for note in result.notes)


async def test_plain_text_is_read_as_text_and_labelled() -> None:
    ingestor = _ingestor(_serve({"/notes.txt": (200, b"just some notes", "text/plain")}))
    result = await ingestor.ingest("https://example.com/notes.txt")
    assert result.text == "just some notes"
    assert any("plain text/plain" in note for note in result.notes)


async def test_a_bare_domain_is_normalised_to_https() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200, content=b"<html><p>x</p></html>", headers={"content-type": "text/html"}
        )

    await _ingestor(handler).ingest("example.com/page")
    assert seen == ["https://example.com/page"]


@pytest.mark.parametrize("bad", ["", "   ", "not-a-link", "@@@"])
async def test_input_that_is_not_a_link_is_a_result_not_a_crash(bad: str) -> None:
    result = await _ingestor(_serve()).ingest(bad)
    assert result.kind == KIND_UNREACHABLE
    assert result.ok is False
    assert result.notes


# ── refusals and failures come back as results ─────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/health",
        "http://core-app:8080/platform/v1/info",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
        "https://admin:pw@example.com/x",
    ],
)
async def test_a_refused_url_returns_an_honest_result(url: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("a refused URL must never be requested")

    result = await _ingestor(handler).ingest(url)
    assert result.kind == KIND_UNREACHABLE
    assert result.ok is False
    assert result.notes[0].startswith("refused:")


async def test_a_login_walled_page_is_reported_as_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/reel/abc":
            return httpx.Response(302, headers={"location": "/accounts/login/?next=/reel/abc"})
        return httpx.Response(
            200,
            content=b"<html><head><title>Log in</title></head><body></body></html>",
            headers={"content-type": "text/html"},
        )

    result = await _ingestor(handler).ingest("https://pictogram.example/reel/abc")
    assert result.kind == KIND_UNREACHABLE
    assert result.ok is False
    assert any("never signs in" in note for note in result.notes)
    assert result.text == ""


async def test_a_403_is_reported_as_a_login_wall_or_block() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    result = await _ingestor(handler).ingest("https://example.com/private")
    assert result.kind == KIND_UNREACHABLE
    assert any("login wall" in note for note in result.notes)


async def test_a_pdf_is_refused_by_content_type_with_a_result() -> None:
    result = await _ingestor(_serve({"/doc.pdf": (200, b"%PDF-1.7", "application/pdf")})).ingest(
        "https://example.com/doc.pdf"
    )
    assert result.kind == KIND_UNREACHABLE
    assert any("application/pdf" in note for note in result.notes)


# ── tier 2: images ─────────────────────────────────────────────────────────────────────


async def test_a_direct_image_link_is_captioned_through_the_core() -> None:
    captioner = _StubCaptioner(caption=Caption(text="A red bicycle against a wall."))
    result = await _ingestor(
        _serve({"/i/bike.png": (200, PNG, "image/png")}), captioner=captioner
    ).ingest("https://example.com/i/bike.png")
    assert result.kind == KIND_IMAGE
    assert result.image_descriptions == ["A red bicycle against a wall."]
    assert result.text == "A red bicycle against a wall."
    assert captioner.calls == ["image/png"]


async def test_an_image_the_model_cannot_see_degrades_to_metadata_plus_a_note() -> None:
    """The #739 vision gate's module half: the result still exists, and it explains itself."""
    captioner = _StubCaptioner(
        caption=Caption(
            note="the image was not described: the configured model (llama3.2) cannot see"
            " images — switch to a vision-capable model to get image descriptions"
        )
    )
    result = await _ingestor(
        _serve({"/i/bike.png": (200, PNG, "image/png")}), captioner=captioner
    ).ingest("https://example.com/i/bike.png")
    assert result.kind == KIND_IMAGE
    assert result.ok is True  # the link was read; only the description is missing
    assert result.image_descriptions == []
    assert any("vision-capable model" in note for note in result.notes)


async def test_an_oversized_image_is_not_sent_to_the_model_at_all() -> None:
    captioner = _StubCaptioner()
    ingestor = _ingestor(
        _serve({"/i/huge.jpg": (200, b"\xff\xd8" + b"\x00" * 5_000, "image/jpeg")}),
        captioner=captioner,
        limits=FetchLimits(max_bytes=100, timeout_s=5.0),
    )
    result = await ingestor.ingest("https://example.com/i/huge.jpg")
    assert captioner.calls == []  # half a JPEG describes nothing
    assert any("larger than the fetch cap" in note for note in result.notes)


# ── tier 3: video / reels ──────────────────────────────────────────────────────────────


def _facts(**kwargs: Any) -> media.MediaFacts:
    return media.MediaFacts(**kwargs)


async def test_video_ingest_merges_probe_oembed_and_opengraph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(_url: str, *, timeout_s: float = 20.0) -> media.MediaFacts:
        return _facts(
            title="Rebuilding a 1962 Riva hull",
            uploader="Slipway Diaries",
            description="Eleven months of restoration.",
            published="2025-04-02",
            duration_s=1215,
        )

    monkeypatch.setattr(media, "probe", fake_probe)

    def handler(request: httpx.Request) -> httpx.Response:
        if "oembed" in str(request.url):
            return httpx.Response(
                200,
                json={"title": "oEmbed title", "author_name": "oEmbed author"},
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, content=VIDEO_PAGE, headers={"content-type": "text/html"})

    ingestor = _ingestor(handler, use_media_probe=True)
    result = await ingestor.ingest("https://www.youtube.com/watch?v=abc123")
    assert result.kind == KIND_VIDEO
    assert result.site == "YouTube"
    # yt-dlp wins over oEmbed, which wins over OpenGraph.
    assert result.title == "Rebuilding a 1962 Riva hull"
    assert result.author == "Slipway Diaries"
    assert result.published == "2025-04-02"
    assert "Eleven months of restoration." in result.text
    assert any("duration: 20:15" in note for note in result.notes)


async def test_uploader_subtitles_are_fetched_and_land_in_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(_url: str, *, timeout_s: float = 20.0) -> media.MediaFacts:
        return _facts(
            title="A clip",
            subtitle_url="https://cdn.youtube.com/subs/en.vtt",
            subtitle_lang="en",
        )

    monkeypatch.setattr(media, "probe", fake_probe)
    vtt = b"WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nThe hull came in on a lorry.\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".vtt"):
            return httpx.Response(200, content=vtt, headers={"content-type": "text/vtt"})
        if "oembed" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=VIDEO_PAGE, headers={"content-type": "text/html"})

    result = await _ingestor(handler, use_media_probe=True).ingest("https://youtu.be/abc123")
    assert "The hull came in on a lorry." in result.text
    assert "Captions (en, published by the uploader)" in result.text
    # Captions are not a transcript — the reserved field stays empty (#739).
    assert result.transcript == ""


async def test_a_video_with_only_machine_captions_says_they_were_not_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(_url: str, *, timeout_s: float = 20.0) -> media.MediaFacts:
        return _facts(title="A clip", description="d", has_only_auto_captions=True)

    monkeypatch.setattr(media, "probe", fake_probe)
    result = await _ingestor(
        _serve({"/watch": (200, VIDEO_PAGE, "text/html")}), use_media_probe=True
    ).ingest("https://www.youtube.com/watch?v=x")
    assert any("machine-generated captions" in note for note in result.notes)


async def test_a_video_with_no_subtitles_says_speech_is_not_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(_url: str, *, timeout_s: float = 20.0) -> media.MediaFacts:
        return _facts(title="A clip", description="d")

    monkeypatch.setattr(media, "probe", fake_probe)
    result = await _ingestor(
        _serve({"/watch": (200, VIDEO_PAGE, "text/html")}), use_media_probe=True
    ).ingest("https://www.youtube.com/watch?v=x")
    assert any("not transcribed yet" in note for note in result.notes)


async def test_the_thumbnail_is_described_through_the_vision_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(_url: str, *, timeout_s: float = 20.0) -> media.MediaFacts:
        return _facts(title="A clip", thumbnail="https://img.example.com/t.jpg")

    monkeypatch.setattr(media, "probe", fake_probe)
    captioner = _StubCaptioner(caption=Caption(text="A wooden boat hull on trestles."))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".jpg"):
            return httpx.Response(200, content=PNG, headers={"content-type": "image/jpeg"})
        if "oembed" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=VIDEO_PAGE, headers={"content-type": "text/html"})

    result = await _ingestor(handler, captioner=captioner, use_media_probe=True).ingest(
        "https://vimeo.com/12345"
    )
    assert result.image_descriptions == ["Thumbnail: A wooden boat hull on trestles."]


async def test_no_thumbnail_call_when_vision_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(_url: str, *, timeout_s: float = 20.0) -> media.MediaFacts:
        return _facts(title="A clip", thumbnail="https://img.example.com/t.jpg")

    monkeypatch.setattr(media, "probe", fake_probe)
    captioner = _StubCaptioner(available=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".jpg"):
            raise AssertionError("the thumbnail must not be fetched when vision is unwired")
        if "oembed" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=VIDEO_PAGE, headers={"content-type": "text/html"})

    await _ingestor(handler, captioner=captioner, use_media_probe=True).ingest(
        "https://vimeo.com/12345"
    )
    assert captioner.calls == []


async def test_a_private_reel_with_nothing_retrievable_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(_url: str, *, timeout_s: float = 20.0) -> None:
        return None

    monkeypatch.setattr(media, "probe", fake_probe)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    result = await _ingestor(handler, use_media_probe=True).ingest(
        "https://www.instagram.com/reel/private1/"
    )
    assert result.kind == KIND_UNREACHABLE
    assert result.ok is False
    assert any("never signs in or uses credentials" in note for note in result.notes)


async def test_platform_chrome_is_not_reported_as_the_posts_own_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Found in live testing: a signed-out Instagram reel came back `published: 2000-01-01`.

    When neither yt-dlp nor oEmbed produced anything, whatever the page yielded is the
    platform's interstitial — its byline and date are artifacts, not facts about the post.
    """

    async def fake_probe(_url: str, *, timeout_s: float = 20.0) -> None:
        return None

    monkeypatch.setattr(media, "probe", fake_probe)
    interstitial = (
        b"<html><head><title>Instagram</title>"
        b"<meta property='og:title' content='Instagram'>"
        b"<meta name='author' content='Instagram'>"
        b"<meta property='article:published_time' content='2013-05-01T00:00:00Z'>"
        b"</head><body></body></html>"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=interstitial, headers={"content-type": "text/html"})

    result = await _ingestor(handler, use_media_probe=True).ingest(
        "https://www.instagram.com/reel/abc/"
    )
    assert result.author is None
    assert result.published is None
    assert any("only the link's basic metadata" in note for note in result.notes)


async def test_real_metadata_survives_when_the_probe_did_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterpart: that drop must not touch a link that genuinely resolved."""

    async def fake_probe(_url: str, *, timeout_s: float = 20.0) -> media.MediaFacts:
        return _facts(title="A clip", uploader="Someone", published="2025-04-02")

    monkeypatch.setattr(media, "probe", fake_probe)

    def handler(request: httpx.Request) -> httpx.Response:
        if "oembed" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=b"<html></html>", headers={"content-type": "text/html"})

    result = await _ingestor(handler, use_media_probe=True).ingest("https://youtu.be/x")
    assert result.author == "Someone"
    assert result.published == "2025-04-02"


async def test_media_probe_is_skipped_when_the_operator_turns_it_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(_url: str, *, timeout_s: float = 20.0) -> media.MediaFacts:
        raise AssertionError("yt-dlp must not run when link_ingest_ytdlp is false")

    monkeypatch.setattr(media, "probe", fake_probe)

    def handler(request: httpx.Request) -> httpx.Response:
        if "oembed" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=VIDEO_PAGE, headers={"content-type": "text/html"})

    result = await _ingestor(handler, use_media_probe=False).ingest("https://youtu.be/x")
    assert result.title == "Rebuilding a 1962 Riva hull"  # OpenGraph carried it


async def test_instagram_falls_back_to_opengraph_without_an_oembed_call() -> None:
    """Meta's token-free oEmbed is gone and #739 forbids authenticating — so we don't try."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=VIDEO_PAGE, headers={"content-type": "text/html"})

    result = await _ingestor(handler).ingest("https://www.instagram.com/reel/abc/")
    assert not any("oembed" in url for url in seen)
    assert result.title == "Rebuilding a 1962 Riva hull"


# ── rendering ──────────────────────────────────────────────────────────────────────────


def test_render_puts_every_field_where_the_model_can_read_it() -> None:
    result = IngestResult(
        kind=KIND_ARTICLE,
        url="https://example.com/a",
        title="A title",
        site="Example",
        author="Someone",
        published="2025-01-02",
        text="Body text.",
        image_descriptions=["A photo."],
        notes=["something was skipped"],
        retrieved_at="2026-08-15",
    )
    text = render(result)
    assert "# A title" in text
    assert "kind: article" in text
    assert "site: Example" in text
    assert "author: Someone" in text
    assert "published: 2025-01-02" in text
    assert "retrieved: 2026-08-15" in text
    assert "source: https://example.com/a" in text
    assert "Body text." in text
    assert "- A photo." in text
    assert "- something was skipped" in text


def test_render_of_an_unreachable_link_still_names_the_link_and_the_reason() -> None:
    result = IngestResult(
        kind=KIND_UNREACHABLE, url="https://example.com/x", ok=False, notes=["refused: nope"]
    )
    text = render(result)
    assert "https://example.com/x" in text
    assert "refused: nope" in text


def test_summary_prefers_the_text_and_falls_back_to_the_first_note() -> None:
    assert IngestResult(kind="page", url="u", text="First line.\nSecond.").summary == "First line."
    assert IngestResult(kind="page", url="u", notes=["why not"]).summary == "why not"
    assert IngestResult(kind="page", url="u").summary == ""
