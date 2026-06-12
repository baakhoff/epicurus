"""Websearch module — MCP tool surface.

Registers one tool the agent can call:

* ``web_search`` — query SearXNG and return ranked web results (title, url,
  snippet, engine) so the agent can answer current-events questions and cite
  sources.
"""

from __future__ import annotations

from epicurus_core import EpicurusModule, UiSection
from epicurus_websearch.searxng import SearchResult, SearXNGClient

MODULE_NAME = "websearch"


def build_module(client: SearXNGClient, max_results: int = 5) -> EpicurusModule:
    """Build the websearch module and register its tools."""
    module = EpicurusModule(
        MODULE_NAME,
        version="0.1.0",
        description="Self-hosted web search via SearXNG — no API key required.",
        ui=UiSection(
            icon="globe",
            summary=(
                "Gives the agent free, private web search via a self-hosted"
                " SearXNG instance. No external API keys required."
            ),
            config_schema={
                "type": "object",
                "properties": {
                    "websearch_max_results": {
                        "type": "integer",
                        "title": "Max results",
                        "description": "Maximum number of results returned per search.",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 5,
                    },
                    "websearch_engines": {
                        "type": "string",
                        "title": "Engines",
                        "description": (
                            "Comma-separated SearXNG engine names to use"
                            " (empty = SearXNG defaults)."
                        ),
                        "default": "",
                    },
                },
            },
            status_url="/status",
        ),
    )

    @module.tool()
    async def web_search(query: str, num_results: int = max_results) -> list[SearchResult]:
        """Search the web for *query* and return ranked results.

        Queries the self-hosted SearXNG instance and returns up to *num_results*
        results, each with ``title``, ``url``, ``snippet``, and ``engine`` fields
        so the agent can cite its sources.

        Args:
            query: Natural-language question or search phrase.
            num_results: Maximum number of results to return (default configured
                by operator; capped at 20).

        Returns a list of ``{title, url, snippet, engine}`` dicts ordered by
        SearXNG's relevance ranking.  Returns an empty list when SearXNG finds
        nothing or is unreachable.
        """
        capped = min(num_results, 20)
        try:
            return await client.search(query, capped)
        except Exception:
            return []

    return module
