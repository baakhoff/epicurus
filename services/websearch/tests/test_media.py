"""Tests for the yt-dlp probe (media.py, #739, tier 3).

yt-dlp itself is never run here — it would mean a live call to a video platform, which is
both flaky and outside what a unit test should touch.  What *is* tested is everything
around it: the host allow-list that decides whether it may run at all, the mapping from its
info dict to :class:`MediaFacts`, and above all the subtitle policy — uploader tracks only,
never the platform's machine-generated captions, because transcription is out of scope and
the two must not be conflated.
"""

from __future__ import annotations

from typing import Any

import pytest

from epicurus_websearch.media import MediaFacts, _facts_from, _pick_subtitle, probe, probe_allowed


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=x",
        "https://youtu.be/x",
        "https://m.youtube.com/watch?v=x",
        "https://vimeo.com/1",
        "https://www.instagram.com/reel/a/",
        "https://www.tiktok.com/@a/video/1",
        "https://soundcloud.com/a/b",
        "https://x.com/a/status/1",
    ],
)
def test_public_media_platforms_may_be_probed(url: str) -> None:
    assert probe_allowed(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/video.mp4",
        "http://core-app:8080/x",
        "https://notyoutube.com/watch?v=x",
        "https://youtube.com.evil.example/watch",
    ],
)
def test_everything_else_is_off_limits_to_ytdlp(url: str) -> None:
    """The allow-list is the containment: yt-dlp fetches outside the SSRF-guarded client."""
    assert probe_allowed(url) is False


async def test_probe_returns_none_without_running_for_a_non_allowlisted_host() -> None:
    assert await probe("https://example.com/some/page") is None


# ── info-dict mapping ──────────────────────────────────────────────────────────────────


def _info(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "  Rebuilding a 1962 Riva hull  ",
        "uploader": "Slipway Diaries",
        "description": "Eleven months of restoration.",
        "upload_date": "20250402",
        "duration": 1215.4,
        "thumbnail": "https://img.example.com/t.jpg",
    }
    base.update(overrides)
    return base


def test_facts_are_mapped_and_trimmed() -> None:
    facts = _facts_from(_info())
    assert facts.title == "Rebuilding a 1962 Riva hull"
    assert facts.uploader == "Slipway Diaries"
    assert facts.published == "2025-04-02"
    assert facts.duration_s == 1215
    assert facts.thumbnail == "https://img.example.com/t.jpg"


def test_uploader_falls_back_to_channel_then_uploader_id() -> None:
    assert _facts_from(_info(uploader=None, channel="A Channel")).uploader == "A Channel"
    assert (
        _facts_from(_info(uploader=None, channel=None, uploader_id="@handle")).uploader == "@handle"
    )


def test_a_missing_or_odd_upload_date_is_passed_through_not_mangled() -> None:
    assert _facts_from(_info(upload_date=None)).published == ""
    assert _facts_from(_info(upload_date="2025")).published == "2025"


def test_a_zero_or_missing_duration_is_not_reported() -> None:
    assert _facts_from(_info(duration=0)).duration_s == 0
    assert _facts_from(_info(duration=None)).duration_s == 0


def test_non_string_fields_do_not_leak_into_the_facts() -> None:
    facts = _facts_from(_info(title=None, description={"nope": 1}, thumbnail=[1, 2]))
    assert facts.title == ""
    assert facts.description == ""
    assert facts.thumbnail == ""


def test_an_empty_info_dict_yields_empty_facts() -> None:
    assert _facts_from({}) == MediaFacts()


# ── the subtitle policy ────────────────────────────────────────────────────────────────


def test_uploader_subtitles_are_picked_english_first() -> None:
    subtitles = {
        "de": [{"ext": "vtt", "url": "https://x/de.vtt"}],
        "en": [{"ext": "vtt", "url": "https://x/en.vtt"}],
    }
    assert _pick_subtitle(subtitles) == ("en", "https://x/en.vtt")


def test_the_preferred_container_wins_within_a_language() -> None:
    subtitles = {
        "en": [
            {"ext": "json3", "url": "https://x/en.json3"},
            {"ext": "vtt", "url": "https://x/en.vtt"},
        ]
    }
    assert _pick_subtitle(subtitles) == ("en", "https://x/en.vtt")


def test_any_language_is_used_when_english_is_absent() -> None:
    assert _pick_subtitle({"fr": [{"ext": "srt", "url": "https://x/fr.srt"}]}) == (
        "fr",
        "https://x/fr.srt",
    )


@pytest.mark.parametrize(
    "subtitles",
    [
        {},
        {"en": []},
        {"en": [{"ext": "vtt"}]},  # no url
        {"en": [{"ext": "exotic", "url": "https://x/en.zzz"}]},
        {"en": "not a list"},
        {"en": ["not a dict"]},
    ],
)
def test_no_usable_track_returns_none(subtitles: dict[str, Any]) -> None:
    assert _pick_subtitle(subtitles) is None


def test_machine_captions_are_flagged_but_never_used() -> None:
    """ASR is out of scope (#739): the platform's own transcription is not the video's text."""
    facts = _facts_from(
        _info(subtitles={}, automatic_captions={"en": [{"ext": "vtt", "url": "https://x/auto"}]})
    )
    assert facts.subtitle_url == ""
    assert facts.has_only_auto_captions is True


def test_uploader_subtitles_take_precedence_and_clear_the_auto_flag() -> None:
    facts = _facts_from(
        _info(
            subtitles={"en": [{"ext": "vtt", "url": "https://x/en.vtt"}]},
            automatic_captions={"en": [{"ext": "vtt", "url": "https://x/auto"}]},
        )
    )
    assert facts.subtitle_url == "https://x/en.vtt"
    assert facts.has_only_auto_captions is False
