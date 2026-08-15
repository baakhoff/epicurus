"""Websearch-service configuration — CoreSettings plus SearXNG and link-ingest fields."""

from __future__ import annotations

from epicurus_core import CoreSettings


class WebSearchSettings(CoreSettings):
    """Adds the SearXNG endpoint, search defaults, and the ``link_ingest`` caps."""

    # Base URL of the SearXNG instance on the internal Docker network.
    searxng_url: str = "http://localhost:8080"
    # Core service base URL (platform API). On the Docker network: http://core-app:8080.
    # Used by link_ingest for vision captioning — all inference goes through the core.
    platform_url: str = "http://localhost:8080"
    # Default maximum number of results the web_search tool returns.
    websearch_max_results: int = 5
    # Comma-separated list of SearXNG engine names to restrict searches to.
    # Empty string means SearXNG uses its default engine set.
    websearch_engines: str = ""

    # ── link_ingest caps (#739) ──────────────────────────────────────────────────────
    # Every one of these bounds a fetch of an *operator-supplied* URL made from inside the
    # Docker network, so the defaults are deliberately conservative: an operator can raise
    # them, but nothing raises them by accident. See epicurus_websearch.safety.
    #
    # Hard ceiling on bytes read per fetch; a longer body is truncated, not failed (a
    # truncated *image* is refused, since half a JPEG cannot be described).
    link_ingest_max_bytes: int = 5_000_000
    # Wall-clock budget for one fetch including every redirect hop.
    link_ingest_timeout_s: float = 20.0
    # Redirect hops followed; each one is re-validated by the SSRF guard before it is taken.
    link_ingest_max_redirects: int = 5
    # Characters of extracted text (and of captions) kept per ingest.
    link_ingest_max_text_chars: int = 20_000
    # Run yt-dlp for richer metadata/subtitles on allow-listed public media platforms.
    # Set false to keep tier 3 on oEmbed + OpenGraph only.
    link_ingest_ytdlp: bool = True
    # Model used for image descriptions; empty means the core's configured default. The
    # module never holds a key — the core resolves and meters the call (constraint #8).
    link_ingest_vision_model: str = ""
    # Identifies these fetches to the sites they hit, rather than pretending to be a browser.
    link_ingest_user_agent: str = "epicurus-websearch (+link_ingest)"
