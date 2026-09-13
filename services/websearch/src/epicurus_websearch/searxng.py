"""Thin async client for the SearXNG JSON search API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict

import httpx


class SearchResult(TypedDict):
    """One result returned by ``web_search``."""

    title: str
    url: str
    snippet: str
    engine: str


@dataclass(frozen=True)
class SearchOutcome:
    """Everything SearXNG's response says about one query — not just the hits.

    A response can answer ``200`` with an empty ``results`` list for two entirely
    different reasons: the query genuinely had no matches, or every engine SearXNG asked
    failed to answer (timed out, was blocked, got rate-limited) and it is reporting that
    honestly rather than erroring. ``unresponsive_engines`` — SearXNG's own
    ``[engine, error_type]`` pairs — is what tells those two apart (#936); an empty
    ``results`` list together with an empty ``unresponsive_engines`` is the only shape
    that means "nothing matched". A non-empty ``unresponsive_engines`` means the search
    cannot be trusted as complete even when ``results`` is non-empty — some engines simply
    did not get a vote.
    """

    results: list[SearchResult]
    unresponsive_engines: list[tuple[str, str]] = field(default_factory=list)
    number_of_results: int = 0


class SearXNGClient:
    """Queries SearXNG's ``/search?format=json`` endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        engines: str = "",
        timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._engines = engines
        self._client = httpx.AsyncClient(timeout=timeout)
        # The most recent search's degraded status, for GET /status to surface without
        # querying SearXNG again — see the module's ``/status`` handler for why this beats
        # a periodic canary query.
        self._last_unresponsive_engines: list[tuple[str, str]] = []
        # Whether any search has completed in this process. An empty
        # ``_last_unresponsive_engines`` means "every engine answered" *or* "nobody has
        # searched yet", and ``/status`` must not report the second as the first.
        self._has_searched = False

    async def search(self, query: str, num_results: int = 5) -> SearchOutcome:
        """Return up to *num_results* web results for *query*, plus engine health.

        Queries SearXNG's JSON endpoint and normalises the response. Empty ``results``
        (e.g. when SearXNG has no engines configured, or a query genuinely has no hits)
        come back as ``SearchOutcome(results=[], unresponsive_engines=[])`` — callers
        distinguish that from a degraded search via ``unresponsive_engines``.

        Raises ``httpx.HTTPError`` on network or HTTP-level failures so callers can
        handle them gracefully (as of #936, that means letting it propagate to the
        tool-error seam — ADR-0136 — rather than swallowing it here).
        """
        params: dict[str, str | int] = {
            "q": query,
            "format": "json",
        }
        if self._engines:
            params["engines"] = self._engines

        resp = await self._client.get(f"{self._base_url}/search", params=params)
        resp.raise_for_status()

        data = resp.json()
        raw_results: list[dict[str, object]] = data.get("results", [])
        out: list[SearchResult] = []
        for item in raw_results[:num_results]:
            title = str(item.get("title") or "")
            url = str(item.get("url") or "")
            snippet = str(item.get("content") or "")
            engine = str(item.get("engine") or "")
            if url:
                out.append(SearchResult(title=title, url=url, snippet=snippet, engine=engine))

        raw_unresponsive = data.get("unresponsive_engines") or []
        unresponsive: list[tuple[str, str]] = [
            (str(pair[0]), str(pair[1]))
            for pair in raw_unresponsive
            if isinstance(pair, (list, tuple)) and len(pair) >= 2
        ]
        number_of_results = int(data.get("number_of_results") or 0)

        self._last_unresponsive_engines = unresponsive
        self._has_searched = True
        return SearchOutcome(
            results=out,
            unresponsive_engines=unresponsive,
            number_of_results=number_of_results,
        )

    @property
    def last_unresponsive_engines(self) -> list[tuple[str, str]]:
        """Engines that failed to answer the most recent successful search, if any.

        Empty before the first search, and cleared by any later search where every
        engine responded. Backs ``GET /status``'s degraded flag (#936/#920) — see that
        handler for why this is preferred over a periodic canary query.
        """
        return list(self._last_unresponsive_engines)

    @property
    def has_searched(self) -> bool:
        """Whether a search has completed in this process — ``/status``'s evidence flag.

        Without it an unsearched instance and a perfectly healthy one are the same shape,
        and the panel would assert "not degraded" on no evidence at all.
        """
        return self._has_searched

    async def health_check(self) -> bool:
        """Return True if SearXNG responds to ``/healthz``."""
        try:
            resp = await self._client.get(f"{self._base_url}/healthz", timeout=3.0)
            return resp.is_success
        except Exception:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
