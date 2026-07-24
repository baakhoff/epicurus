"""Websearch module — MCP tool surface.

Registers one tool the agent can call:

* ``web_search`` — query SearXNG and return ranked web results (title, url,
  snippet, engine) so the agent can answer current-events questions, ground
  anything it cannot source locally (#703), and cite sources. Each result
  also becomes a chat entity-reference chip (#551,
  ADR-0019) the operator can hover for a preview and click to open in a new
  tab — resolved statelessly via ``epicurus_websearch.refs``.
"""

from __future__ import annotations

from epicurus_core import EntityRef, EpicurusModule, UiSection, capped_listing, tool_envelope
from epicurus_websearch.refs import RESULT_KIND, canonical_url, encode_ref
from epicurus_websearch.searxng import SearchResult, SearXNGClient

MODULE_NAME = "websearch"


def _dedupe_by_url(results: list[SearchResult]) -> list[SearchResult]:
    """Collapse same-page duplicates SearXNG can return from multiple engines.

    Keeps the first occurrence. This is the intra-call half of "de-dupe
    identical URLs" (#551); the cross-call half — two separate ``web_search``
    calls in one turn surfacing the same page — relies on both calls encoding
    an identical ``ref_id`` for it, which holds as long as SearXNG returns the
    same title/snippet/engine for the same URL within the turn (the common
    case; see ``refs.encode_ref``).
    """
    seen: set[str] = set()
    out: list[SearchResult] = []
    for result in results:
        key = canonical_url(result["url"])
        if key in seen:
            continue
        seen.add(key)
        out.append(result)
    return out


def build_module(client: SearXNGClient, max_results: int = 5) -> EpicurusModule:
    """Build the websearch module and register its tools."""
    module = EpicurusModule(
        MODULE_NAME,
        version="0.2.1",
        description="Self-hosted web search via SearXNG — no API key required.",
        resolver=True,
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
    async def web_search(query: str, num_results: int = max_results) -> str:
        """Search the web for *query* and return ranked results.

        Reach for this whenever the answer is not in the operator's own data
        or could have changed since training: current events, releases,
        prices, schedules, or any fact you cannot ground in a local source.
        Prefer searching over answering from memory — never guess when you
        can look something up.

        Queries the self-hosted SearXNG instance and returns up to *num_results*
        results, each with title, URL, and snippet, so the agent can cite its
        sources.  Each result also becomes a "Sources" chip in the chat UI —
        hover for a preview, click to open the page in a new tab.

        Args:
            query: Natural-language question or search phrase.
            num_results: Maximum number of results to return (default configured
                by operator; capped at 20).

        Returns an entity-ref-carrying envelope ranked by SearXNG's relevance.
        Reports no results found when SearXNG finds nothing or is unreachable,
        rather than failing the turn.
        """
        capped = min(num_results, 20)
        try:
            results = await client.search(query, capped)
        except Exception:
            results = []

        deduped = _dedupe_by_url(results)
        if not deduped:
            return tool_envelope("No web results found.", [])

        refs = [
            EntityRef(
                ref_id=encode_ref(
                    url=r["url"], title=r["title"], snippet=r["snippet"], engine=r["engine"]
                ),
                module=MODULE_NAME,
                kind=RESULT_KIND,
                title=r["title"],
                summary=r["snippet"],
            )
            for r in deduped
        ]
        lines = [
            f"- {r['title']} — {r['url']} (via {r['engine']})\n  {r['snippet']}" for r in deduped
        ]
        text = capped_listing(lines, noun="result")
        return tool_envelope(text, refs)

    return module
