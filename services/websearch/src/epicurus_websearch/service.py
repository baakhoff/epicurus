"""Websearch module — MCP tool surface.

Registers two tools the agent can call:

* ``web_search`` — query SearXNG and return ranked web results (title, url,
  snippet, engine) so the agent can answer current-events questions, ground
  anything it cannot source locally (#703), and cite sources. Each result
  also becomes a chat entity-reference chip (#551,
  ADR-0019) the operator can hover for a preview and click to open in a new
  tab — resolved statelessly via ``epicurus_websearch.refs``.
* ``link_ingest`` — read one link and return what is actually behind it (#739):
  an article's text and byline, an image's description, a video's metadata and
  the uploader's own subtitles. Search finds pages; this one *reads* them.
  Deterministic extraction happens here; the single model call it can make
  (describing an image) goes through the core's LLM gateway, never a provider
  (constraint #8). The module calls no other module (ADR-0004) — filing the
  result into the knowledge base is the agent's own next step.
"""

from __future__ import annotations

from epicurus_core import EntityRef, EpicurusModule, UiSection, capped_listing, tool_envelope
from epicurus_websearch.ingest import LinkIngestor, render
from epicurus_websearch.refs import RESULT_KIND, SOURCE_KIND, canonical_url, encode_ref
from epicurus_websearch.refs import encode_source_ref as encode_source
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


def build_module(
    client: SearXNGClient,
    max_results: int = 5,
    *,
    ingestor: LinkIngestor | None = None,
) -> EpicurusModule:
    """Build the websearch module and register its tools.

    ``ingestor`` backs ``link_ingest``; ``None`` still registers the tool but every call
    reports that link reading is not configured, so the manifest is the same shape whether
    or not the service wired one up.
    """
    module = EpicurusModule(
        MODULE_NAME,
        version="0.3.1",
        description=(
            "Self-hosted web search via SearXNG, plus guarded reading of any link —"
            " no API key required."
        ),
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

    @module.tool()
    async def link_ingest(url: str) -> str:
        """Read the page behind *url* and return what is actually in it.

        Reach for this whenever a link turns up and its contents matter — the operator
        pastes one and says "save this", "summarise this", "what's in this reel", or a
        `web_search` result looks like the answer. A search snippet is not the page: read
        the link before you summarise, quote, or file it, rather than answering from the
        snippet or from what you remember about the site.

        Handles three kinds of link. **Articles and ordinary pages**: title, byline,
        publication date, and the main body text, with the navigation and boilerplate
        stripped. **Images**: a description produced by the core's vision model, when the
        operator has one configured. **Public video, reel, and audio links**: title,
        uploader, description, and the uploader's own subtitles where the platform
        publishes them.

        It never signs in, never uses credentials, and never works around a block — so
        private and login-walled links come back marked as unreachable, with a note saying
        so, and never a guess at what they might have contained. Speech is likewise
        not transcribed yet: a video with no uploader subtitles yields its metadata and
        nothing of what was said aloud. Read the result's notes and tell the operator
        plainly what was and was not retrieved; do not fill a gap the tool reported.

        Having read it, do something with it: answer from it, or — when the operator wants
        it kept — write it up in your own words and file it with `knowledge_propose_edit`,
        keeping the source URL and the retrieval date in the document so the claim stays
        traceable.

        Args:
            url: An http(s) link. Private, loopback, and internal-network addresses are
                refused, as are links carrying credentials.

        Returns an entity-ref-carrying envelope: a labelled block with the link's kind,
        title, site, author, date, extracted text, any image descriptions, and the notes.
        An unreadable link is a normal result explaining why, never a failed turn.
        """
        if ingestor is None:
            return tool_envelope(
                "Link reading is not configured on this deployment — the websearch module"
                " could not reach the core to set it up.",
                [],
            )
        result = await ingestor.ingest(url)
        ref = EntityRef(
            ref_id=encode_source(
                url=result.url,
                title=result.title or result.url,
                summary=result.summary,
                kind=result.kind,
                site=result.site,
            ),
            module=MODULE_NAME,
            kind=SOURCE_KIND,
            title=result.title or result.url,
            summary=result.summary,
        )
        return tool_envelope(render(result), [ref])

    return module
