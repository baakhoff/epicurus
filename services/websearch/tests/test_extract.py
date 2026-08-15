"""Tests for deterministic extraction (extract.py, #739).

Fixture HTML, never a live fetch: the pages the tool meets in production are recorded here
(``tests/fixtures/``) so the extraction contract is pinned and the suite stays offline —
live external fetches are flaky and would make a red run mean nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from epicurus_websearch.extract import (
    extract_page,
    host_of,
    looks_login_walled,
    media_platform,
    oembed_endpoint,
    parse_oembed,
    parse_vtt,
    plausible_author,
    plausible_date,
)

FIXTURES = Path(__file__).parent / "fixtures"
ARTICLE = (FIXTURES / "article.html").read_text(encoding="utf-8")
VIDEO_PAGE = (FIXTURES / "video_page.html").read_text(encoding="utf-8")


# ── tier 1: articles ───────────────────────────────────────────────────────────────────


def test_article_metadata_comes_out_of_opengraph() -> None:
    facts = extract_page(ARTICLE, url="https://coastalreview.example/a/tidal")
    assert facts.title == "Tidal turbines feed a village for a winter"
    assert facts.site == "The Coastal Review"
    assert facts.author == "Marit Halvorsen"
    assert facts.published == "2025-11-14"
    assert facts.og_type == "article"


def test_article_body_is_extracted_without_the_furniture() -> None:
    facts = extract_page(ARTICLE, url="https://coastalreview.example/a/tidal")
    assert "Kilbrannan Sound" in facts.text
    assert "gravity base" in facts.text
    # Navigation, consent banner, newsletter box, related rail, and footer all dropped.
    for noise in ("Accept all?", "Sign up for our weekly", "All rights reserved", "Subscribe"):
        assert noise not in facts.text


def test_article_text_is_capped_and_the_cap_is_disclosed() -> None:
    """A truncated extract must never look complete — the note is the honesty half."""
    facts = extract_page(ARTICLE, url="https://coastalreview.example/a/tidal", max_chars=200)
    assert len(facts.text) <= 200
    assert any("first 200 characters" in note for note in facts.notes)


def test_lead_image_is_picked_up_for_the_vision_path() -> None:
    facts = extract_page(ARTICLE, url="https://coastalreview.example/a/tidal")
    assert facts.image == "https://cdn.coastalreview.example/turbines-lede.jpg"


def test_title_falls_back_to_the_title_tag_when_there_is_no_opengraph() -> None:
    html = "<html><head><title>  Plain   Old  Page </title></head><body><p>x</p></body></html>"
    facts = extract_page(html, url="https://example.com/p")
    assert facts.title == "Plain Old Page"


def test_site_falls_back_to_the_host() -> None:
    facts = extract_page("<html><body><p>hi</p></body></html>", url="https://www.example.com/p")
    assert facts.site == "example.com"


@pytest.mark.parametrize(
    "html",
    [
        "",
        "not html at all",
        "<html><body>",
        "<<<>>>",
        "<html><head><title></title></head>",
        "\x00\x01\x02",
    ],
)
def test_malformed_documents_degrade_rather_than_raising(html: str) -> None:
    """Garbage in must not end the turn: whatever survives, plus the host, is the answer."""
    facts = extract_page(html, url="https://example.com/p")
    assert facts.site == "example.com"
    assert isinstance(facts.text, str)
    assert facts.author is None


# ── metadata plausibility (found by live testing) ──────────────────────────────────────
#
# A heuristic extractor is confidently wrong sometimes, and its output is copied verbatim
# into whatever the agent files. Both filters below exist because of a real observed case:
# Wikipedia's nav block coming back as the author, and a signed-out Instagram interstitial
# coming back with `published: 2000-01-01`. An absent field is honest; an invented one is not.


@pytest.mark.parametrize(
    "author", ["Marit Halvorsen", "By Jane Q. Public", "A. Author; B. Writer", "Cher"]
)
def test_real_bylines_survive(author: str) -> None:
    assert plausible_author(author) == author


@pytest.mark.parametrize(
    "author",
    [
        "Authority control databases International GND National Japan",  # observed live
        "Home News Sport Business Opinion Culture Travel More",
        "x" * 81,
        "",
    ],
)
def test_navigation_text_is_not_reported_as_an_author(author: str) -> None:
    assert plausible_author(author) is None


def test_a_multi_author_credit_is_judged_per_name() -> None:
    """One over-long run poisons the field; several ordinary names do not."""
    assert plausible_author("Ann Lee; Bo Ng; Cy Oh") == "Ann Lee; Bo Ng; Cy Oh"
    assert plausible_author("Ann Lee; a whole sentence of navigation text lives here too") is None


@pytest.mark.parametrize("date", ["2025-11-14", "1996-01-01", "Spring 2024"])
def test_believable_dates_survive(date: str) -> None:
    assert plausible_date(date) == date


@pytest.mark.parametrize("date", ["1900-01-01", "1970-01-01", "3025-01-01", ""])
def test_impossible_dates_are_dropped(date: str) -> None:
    assert plausible_date(date) is None


def test_the_extractor_does_not_invent_a_date_for_a_content_free_page() -> None:
    """`extensive=False`: no explicit date on the page means no date, not a guess."""
    html = (
        "<html><head><title>Someplace</title></head>"
        "<body><div id='app'></div><footer>© 2000</footer></body></html>"
    )
    facts = extract_page(html, url="https://someplace.example/p/abc")
    assert facts.published is None


# ── tier 3: media pages ────────────────────────────────────────────────────────────────


def test_video_page_metadata_survives_an_empty_body() -> None:
    facts = extract_page(VIDEO_PAGE, url="https://cliptube.example/w/abc123")
    assert facts.title == "Rebuilding a 1962 Riva hull"
    assert facts.site == "ClipTube"
    assert facts.og_type == "video.other"
    assert facts.description.startswith("Eleven months")
    assert facts.image == "https://img.cliptube.example/thumb/abc123.jpg"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=x", "YouTube"),
        ("https://youtu.be/x", "YouTube"),
        ("https://m.youtube.com/watch?v=x", "YouTube"),
        ("https://www.instagram.com/reel/abc/", "Instagram"),
        ("https://vimeo.com/12345", "Vimeo"),
        ("https://www.tiktok.com/@a/video/1", "TikTok"),
        ("https://x.com/someone/status/1", "X"),
        ("https://example.com/article", None),
        ("https://notyoutube.com/watch", None),
    ],
)
def test_media_platform_detection(url: str, expected: str | None) -> None:
    assert media_platform(url) == expected


def test_oembed_endpoint_only_for_platforms_that_answer_without_a_token() -> None:
    assert oembed_endpoint("https://youtu.be/x") == "https://www.youtube.com/oembed"
    assert oembed_endpoint("https://vimeo.com/1") == "https://vimeo.com/api/oembed.json"
    # Meta retired the token-free endpoints in 2020; #739 forbids authenticating, so these
    # deliberately have none and fall back to OpenGraph.
    assert oembed_endpoint("https://www.instagram.com/reel/abc/") is None
    assert oembed_endpoint("https://www.facebook.com/watch/?v=1") is None
    assert oembed_endpoint("https://example.com/a") is None


def test_parse_oembed_keeps_the_fields_we_use() -> None:
    payload = (
        b'{"title": "A clip", "author_name": "Someone", "provider_name": "ClipTube",'
        b' "thumbnail_url": "https://img/x.jpg", "type": "video", "width": 640}'
    )
    parsed = parse_oembed(payload)
    assert parsed["title"] == "A clip"
    assert parsed["author_name"] == "Someone"
    assert parsed["thumbnail_url"] == "https://img/x.jpg"
    assert "width" not in parsed  # non-string fields are dropped, not stringified


@pytest.mark.parametrize(
    "payload", [b"<html>an error page</html>", b"[1,2,3]", b"", b"\xff\xfe not utf8"]
)
def test_parse_oembed_returns_empty_for_anything_that_is_not_a_json_object(
    payload: bytes,
) -> None:
    assert parse_oembed(payload) == {}


# ── captions ───────────────────────────────────────────────────────────────────────────

VTT = b"""WEBVTT
Kind: captions
Language: en

1
00:00:01.000 --> 00:00:04.000
<v Narrator>The hull came in on a lorry from Cannes.

2
00:00:04.000 --> 00:00:07.500
The hull came in on a lorry from Cannes.
Eleven months later it went back on the water.

3
00:00:07.500 --> 00:00:09.000
Eleven months later it went back on the water.
"""


def test_parse_vtt_returns_prose_without_timings_or_cue_numbers() -> None:
    text = parse_vtt(VTT)
    assert "The hull came in on a lorry from Cannes." in text
    assert "-->" not in text
    assert "WEBVTT" not in text
    assert "Kind:" not in text
    assert "<v " not in text


def test_parse_vtt_collapses_the_rolling_duplicate_lines() -> None:
    """Rolling captions repeat each line as the window scrolls; once is enough for a model."""
    text = parse_vtt(VTT)
    assert text.count("The hull came in on a lorry from Cannes.") == 1
    assert text.count("Eleven months later it went back on the water.") == 1


def test_parse_vtt_respects_the_character_cap() -> None:
    assert len(parse_vtt(VTT, max_chars=30)) <= 30


def test_parse_vtt_of_an_empty_track_is_empty() -> None:
    assert parse_vtt(b"WEBVTT\n\n") == ""


# ── login walls & hosts ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/accounts/login/?next=/reel/abc/",
        "https://example.com/login",
        "https://example.com/auth/login?return=/x",
        "https://example.com/sign-in",
    ],
)
def test_login_walls_are_recognised_from_the_landing_url(url: str) -> None:
    assert looks_login_walled(url) is True


def test_a_normal_article_url_is_not_a_login_wall() -> None:
    assert looks_login_walled("https://example.com/a/tidal-turbines") is False


def test_login_wall_recognised_from_the_page_title() -> None:
    assert looks_login_walled("https://example.com/p", "Log in • Instagram") is True


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://WWW.Example.COM/p", "example.com"),
        ("https://sub.example.com./p", "sub.example.com"),
        ("https://example.com", "example.com"),
        ("not a url", ""),
    ],
)
def test_host_of_normalises(url: str, host: str) -> None:
    assert host_of(url) == host
