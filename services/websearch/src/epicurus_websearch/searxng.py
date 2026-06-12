"""Thin async client for the SearXNG JSON search API."""

from __future__ import annotations

from typing import TypedDict

import httpx


class SearchResult(TypedDict):
    """One result returned by ``web_search``."""

    title: str
    url: str
    snippet: str
    engine: str


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

    async def search(self, query: str, num_results: int = 5) -> list[SearchResult]:
        """Return up to *num_results* web results for *query*.

        Queries SearXNG's JSON endpoint and normalises the response into a flat
        list.  Empty results (e.g. when SearXNG has no engines configured) return
        ``[]``.

        Raises ``httpx.HTTPError`` on network or HTTP-level failures so callers
        can handle them gracefully.
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
        return out

    async def health_check(self) -> bool:
        """Return True if SearXNG responds to ``/healthz``."""
        try:
            resp = await self._client.get(f"{self._base_url}/healthz", timeout=3.0)
            return resp.is_success
        except Exception:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
