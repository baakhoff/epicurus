"""Public-platform video/audio metadata, via yt-dlp (#739, tier 3).

What this does and does not do, because the line matters:

* **Metadata and the uploader's own subtitles only.** Nothing is ever downloaded — no
  video, no audio, no ffmpeg, no OS packages added to the image.  ``extract_info`` runs
  with ``download=False``.
* **Uploader subtitles, not machine captions.**  ``automatic_captions`` (the platform's own
  speech recognition) is deliberately ignored: transcription is out of scope for #739, and
  passing a platform's ASR off as the video's text would blur exactly the line the tool
  contract draws.  When only automatic captions exist the result says so.
* **No sign-in, ever.**  No cookies, no cookie jar, no netrc, no credentials of any kind
  (#739's honesty rule).  A private or login-walled video fails, and the failure is
  reported as a failure.
* **Allow-listed hosts only.** yt-dlp does its own HTTP, outside
  :class:`~epicurus_websearch.safety.GuardedFetcher`, so it never sees an arbitrary
  operator-supplied URL — only one whose host is a known public media platform
  (:data:`~epicurus_websearch.extract.MEDIA_HOSTS`) *and* which already passed the SSRF
  guard.  Subtitle URLs it hands back are fetched through the guarded fetcher like anything
  else.

The import is lazy and failure-tolerant: yt-dlp ships in the image, but an operator who
strips it (or a wheel that will not import on their platform) degrades to oEmbed +
OpenGraph rather than breaking the tool.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from epicurus_core import get_logger
from epicurus_websearch.extract import MEDIA_HOSTS, host_of

log = get_logger("epicurus_websearch.media")

# Subtitle containers we can turn into prose, best first.
_SUBTITLE_FORMATS = ("vtt", "srt", "ttml", "srv3")


@dataclass
class MediaFacts:
    """What a probe learned about a video/audio link. Empty fields mean "not available"."""

    title: str = ""
    uploader: str = ""
    description: str = ""
    published: str = ""
    duration_s: int = 0
    thumbnail: str = ""
    subtitle_url: str = ""
    subtitle_lang: str = ""
    has_only_auto_captions: bool = False
    notes: list[str] = field(default_factory=list)


def probe_allowed(url: str) -> bool:
    """Whether ``url``'s host is one of the public media platforms yt-dlp may be run against."""
    host = host_of(url)
    return any(host == suffix or host.endswith("." + suffix) for suffix in MEDIA_HOSTS)


async def probe(url: str, *, timeout_s: float = 20.0) -> MediaFacts | None:
    """Metadata for a public media link, or ``None`` when nothing could be learned.

    Runs the blocking yt-dlp extractor in a worker thread. ``timeout_s`` bounds *the tool* —
    it cannot kill the thread, so yt-dlp's own ``socket_timeout`` is set alongside it to
    bound the thread; both are needed and neither is redundant.
    """
    if not probe_allowed(url):
        return None
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_probe_sync, url, timeout_s), timeout=timeout_s + 1.0
        )
    except TimeoutError:
        log.info("media probe timed out", url=url)
        facts = MediaFacts()
        facts.notes.append("the platform took too long to answer, so metadata may be incomplete")
        return facts


def _probe_sync(url: str, timeout_s: float) -> MediaFacts | None:
    try:
        from yt_dlp import YoutubeDL
    except Exception:  # pragma: no cover — only when the optional dep is stripped
        log.info("yt-dlp unavailable; falling back to oEmbed/OpenGraph")
        return None

    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": max(1.0, timeout_s / 2),
        "retries": 1,
        # Constraint #2: services keep no local state. Also keeps a cache poisoning vector
        # off the table entirely.
        "cachedir": False,
        # #739's honesty rule, made structural rather than a promise: no credential source
        # of any kind is offered to the extractor.
        "cookiefile": None,
        "cookiesfrombrowser": None,
        "usenetrc": False,
        "username": None,
        "password": None,
    }
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        # A private video, a geo-block, an extractor that broke overnight — all the same to
        # us: report nothing rather than guess. The caller still has oEmbed/OpenGraph.
        log.info("media probe failed", url=url, error=str(exc)[:200])
        return None
    if not isinstance(info, dict):
        return None
    return _facts_from(info)


def _facts_from(info: dict[str, Any]) -> MediaFacts:
    facts = MediaFacts(
        title=_text(info.get("title")),
        uploader=_text(info.get("uploader") or info.get("channel") or info.get("uploader_id")),
        description=_text(info.get("description")),
        published=_date(info.get("upload_date")),
        thumbnail=_text(info.get("thumbnail")),
    )
    duration = info.get("duration")
    if isinstance(duration, int | float) and duration > 0:
        facts.duration_s = int(duration)
    subtitles = info.get("subtitles")
    picked = _pick_subtitle(subtitles) if isinstance(subtitles, dict) else None
    if picked is not None:
        facts.subtitle_lang, facts.subtitle_url = picked
    else:
        auto = info.get("automatic_captions")
        facts.has_only_auto_captions = isinstance(auto, dict) and bool(auto)
    return facts


def _pick_subtitle(subtitles: dict[str, Any]) -> tuple[str, str] | None:
    """The best uploader-provided subtitle track: English if offered, else the first one."""
    languages = sorted(subtitles, key=lambda lang: (not lang.lower().startswith("en"), lang))
    for language in languages:
        tracks = subtitles.get(language)
        if not isinstance(tracks, list):
            continue
        for wanted in _SUBTITLE_FORMATS:
            for track in tracks:
                if not isinstance(track, dict):
                    continue
                if str(track.get("ext", "")).lower() == wanted and track.get("url"):
                    return language, str(track["url"])
    return None


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _date(value: Any) -> str:
    """yt-dlp's ``YYYYMMDD`` upload date, as ISO ``YYYY-MM-DD``."""
    raw = _text(value)
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw
