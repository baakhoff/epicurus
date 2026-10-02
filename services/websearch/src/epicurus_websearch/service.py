"""Websearch module — MCP tool surface.

Registers two tools the agent can call:

* ``web_search`` — search the web and return ranked results (title, url,
  snippet, engine) through the operator's chosen provider: the bundled SearXNG
  (default), or OpenRouter's hosted web search run by the core with the tenant's
  stored OpenRouter key (#984 — the module never holds the key), so the agent can
  answer current-events questions, ground anything it cannot source locally (#703),
  and cite sources. Each result also becomes a chat entity-reference chip (#551,
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

from epicurus_core import (
    EntityRef,
    EpicurusModule,
    PlatformClient,
    PlatformError,
    UiSection,
    capped_listing,
    get_logger,
    tool_envelope,
)
from epicurus_websearch.config import (
    OPENROUTER,
    ConfigSource,
    EffectiveConfig,
    static_source,
)
from epicurus_websearch.ingest import LinkIngestor, render
from epicurus_websearch.refs import RESULT_KIND, SOURCE_KIND, canonical_url, encode_ref
from epicurus_websearch.refs import encode_source_ref as encode_source
from epicurus_websearch.searxng import SearchResult, SearXNGClient

MODULE_NAME = "websearch"

logger = get_logger(__name__)


def describe_unresponsive(unresponsive: list[tuple[str, str]]) -> str:
    """Render SearXNG's ``[engine, error_type]`` pairs as ``"bing (timeout), google (blocked)"``.

    Public because ``GET /status`` renders the same pairs the same way: a module's status
    fields are flat scalars (the shell stringifies each one), so the panel and the tool
    message say the identical thing rather than one of them showing ``[object Object]``.
    """
    return ", ".join(f"{engine} ({error})" for engine, error in unresponsive)


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


KEY_MISSING = "openrouter_key_missing"
"""The core's ``detail.code`` for "OpenRouter is chosen but no key is stored" (#984)."""

NO_KEY_MESSAGE = (
    "Web search is set to use OpenRouter, but no OpenRouter API key is stored, so no search"
    " ran. This is not an empty result — do not tell the operator nothing was found. Tell them"
    " web search is unavailable until they either add an OpenRouter key on the Models page or"
    " switch web search back to SearXNG in the websearch module's settings on the Modules page."
)

DEGRADED_NO_SEARCH_MESSAGE = (
    "Search is degraded, not confirmed empty: OpenRouter answered, but its search model did not"
    " run a web search this time. Do not report this as a clean 'no results' — say plainly that"
    " search did not run, and consider retrying once before falling back to anything else."
)


class BackendStatus:
    """What the last OpenRouter-backed search came to, for ``GET /status`` (#984).

    SearXNG keeps its own evidence on its client; this is the OpenRouter half, one flat string
    so the panel renders it as-is: ``None`` until an OpenRouter search has run in this process,
    then ``results`` / ``no results`` / ``no search ran`` / ``no OpenRouter key stored`` /
    ``failed: <the core's reason>``.
    """

    def __init__(self) -> None:
        self.openrouter_last_result: str | None = None


def _render_results(results: list[SearchResult]) -> tuple[str, list[EntityRef]]:
    """The listing and the chips for a non-empty result list — one shape for every backend."""
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
        for r in results
    ]
    lines = [f"- {r['title']} — {r['url']} (via {r['engine']})\n  {r['snippet']}" for r in results]
    return capped_listing(lines, noun="result"), refs


def build_module(
    client: SearXNGClient,
    max_results: int = 5,
    *,
    ingestor: LinkIngestor | None = None,
    config: ConfigSource | None = None,
    platform: PlatformClient | None = None,
    status: BackendStatus | None = None,
) -> EpicurusModule:
    """Build the websearch module and register its tools.

    ``ingestor`` backs ``link_ingest``; ``None`` still registers the tool but every call
    reports that link reading is not configured, so the manifest is the same shape whether
    or not the service wired one up.

    ``config`` answers the effective settings for each call — the operator's stored choice
    over env defaults (``epicurus_websearch.config``, #984); ``None`` means "SearXNG with
    ``max_results`` and the client's engines", exactly the pre-#984 module. ``platform`` is
    how an OpenRouter-backed search reaches the core, which holds the key (constraint #8).
    """
    source = config or static_source(EffectiveConfig(max_results=max_results))
    backend_status = status or BackendStatus()
    module = EpicurusModule(
        MODULE_NAME,
        version="0.5.0",
        description=(
            "Web search via the bundled, self-hosted SearXNG — or, once an OpenRouter key is"
            " stored, OpenRouter's hosted web search — plus guarded reading of any link."
        ),
        resolver=True,
        ui=UiSection(
            icon="globe",
            summary=(
                "Gives the agent web search. By default it uses the bundled, self-hosted"
                " SearXNG: free, private, no API key. With an OpenRouter key stored on the"
                " Models page you can switch it to OpenRouter's hosted web search instead."
            ),
            config_schema={
                "type": "object",
                "properties": {
                    "websearch_backend": {
                        "type": "string",
                        "title": "Search provider",
                        "description": (
                            "Where web searches run. SearXNG is the bundled, self-hosted"
                            " search. OpenRouter runs each search through OpenRouter's web"
                            " search with your stored OpenRouter key (billed to that account)."
                        ),
                        "enum": ["searxng", OPENROUTER],
                        "enumLabels": ["SearXNG (self-hosted)", "OpenRouter web search"],
                        # Parallel to `enum`: the provider whose stored key an option needs
                        # (#984). The shell greys the option out without it; the core refuses
                        # to save it without it.
                        "enumRequiresProviderKey": [None, OPENROUTER],
                        "default": "searxng",
                    },
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
                            " (empty = SearXNG defaults). Applies to SearXNG only."
                        ),
                        "default": "",
                    },
                },
            },
            status_url="/status",
        ),
    )

    async def _search_openrouter(query: str, limit: int) -> str:
        """One search through the core's OpenRouter path — the same three outcomes as SearXNG."""
        if platform is None:
            backend_status.openrouter_last_result = "failed: the core is not configured"
            return tool_envelope(
                "Web search is set to use OpenRouter, but this module has no route to the core"
                " to run it. No search ran — say so plainly rather than reporting no results.",
                [],
            )
        try:
            answer = await platform.web_search(query, max_results=limit)
        except PlatformError as exc:
            if exc.code == KEY_MISSING:
                backend_status.openrouter_last_result = "no OpenRouter key stored"
                logger.warning("openrouter web search selected but no key is stored")
                return tool_envelope(NO_KEY_MESSAGE, [])
            backend_status.openrouter_last_result = f"failed: {exc.message}"
            # Through the tool-error seam (ADR-0136): the core's own sentence reaches the model.
            raise
        results = _dedupe_by_url(
            [
                SearchResult(title=h.title, url=h.url, snippet=h.snippet, engine=h.engine)
                for h in answer.results
            ]
        )
        if not results:
            if answer.searched is False:
                backend_status.openrouter_last_result = "no search ran"
                return tool_envelope(DEGRADED_NO_SEARCH_MESSAGE, [])
            backend_status.openrouter_last_result = "no results"
            return tool_envelope("No web results found.", [])
        backend_status.openrouter_last_result = "results"
        text, refs = _render_results(results)
        return tool_envelope(text, refs)

    @module.tool()
    async def web_search(query: str, num_results: int | None = None) -> str:
        """Search the web for *query* and return ranked results.

        Reach for this whenever the answer is not in the operator's own data
        or could have changed since training: current events, releases,
        prices, schedules, or any fact you cannot ground in a local source.
        Prefer searching over answering from memory — never guess when you
        can look something up.

        Searches with the provider the operator chose — the self-hosted SearXNG instance, or
        OpenRouter's hosted web search — and returns up to *num_results* results, each with
        title, URL, and snippet, so the agent can cite its sources.  Each result also becomes
        a "Sources" chip in the chat UI — hover for a preview, click to open the page in a new
        tab.

        Args:
            query: Natural-language question or search phrase.
            num_results: Maximum number of results to return (omit for the operator's
                configured default; capped at 20).

        Returns an entity-ref-carrying envelope of ranked results. Three distinguishable
        outcomes reach you: results; a genuine "no results found" when the query truly had
        no matches; and a degraded-search note when the search provider answered but could
        not search fully (some engines did not respond, or no search ran) — treat that one as
        "search may be unreliable right now", not as a confirmed empty result. If the chosen
        provider is not set up (OpenRouter chosen with no key stored) the reply says so and
        no search ran. If the provider itself is unreachable or erroring, this tool raises
        rather than reporting a silent empty search.
        """
        effective = await source()
        capped = min(num_results if num_results is not None else effective.max_results, 20)
        capped = max(capped, 1)
        if effective.backend == OPENROUTER:
            return await _search_openrouter(query, capped)

        outcome = await client.search(query, capped, engines=effective.engines)

        if outcome.unresponsive_engines:
            logger.warning(
                "search engines did not respond",
                unresponsive_engines=outcome.unresponsive_engines,
                query_had_results=bool(outcome.results),
            )

        deduped = _dedupe_by_url(outcome.results)
        if not deduped:
            if outcome.unresponsive_engines:
                return tool_envelope(
                    "Search is degraded, not confirmed empty: no results came back, and "
                    f"{len(outcome.unresponsive_engines)} search engine(s) did not respond"
                    f" ({describe_unresponsive(outcome.unresponsive_engines)}). Do not report"
                    " this as a clean 'no results' — say plainly that search is currently"
                    " unreliable or unavailable, and consider retrying before falling back to"
                    " anything else.",
                    [],
                )
            return tool_envelope("No web results found.", [])

        text, refs = _render_results(deduped)
        if outcome.unresponsive_engines:
            text += (
                f"\n\n(Note: {len(outcome.unresponsive_engines)} search engine(s) did not"
                f" respond — {describe_unresponsive(outcome.unresponsive_engines)} — these"
                " results may be incomplete.)"
            )
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
