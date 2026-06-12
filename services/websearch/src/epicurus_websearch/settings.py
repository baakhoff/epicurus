"""Websearch-service configuration — CoreSettings plus SearXNG-specific fields."""

from __future__ import annotations

from epicurus_core import CoreSettings


class WebSearchSettings(CoreSettings):
    """Adds SearXNG endpoint and search defaults to shared settings."""

    # Base URL of the SearXNG instance on the internal Docker network.
    searxng_url: str = "http://localhost:8080"
    # Core service base URL (platform API). On the Docker network: http://core-app:8080.
    platform_url: str = "http://localhost:8080"
    # Default maximum number of results the web_search tool returns.
    websearch_max_results: int = 5
    # Comma-separated list of SearXNG engine names to restrict searches to.
    # Empty string means SearXNG uses its default engine set.
    websearch_engines: str = ""
