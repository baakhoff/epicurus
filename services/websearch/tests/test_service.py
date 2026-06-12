"""Unit tests for the websearch MCP module surface."""

from __future__ import annotations

from unittest.mock import AsyncMock

from epicurus_websearch.searxng import SearchResult, SearXNGClient
from epicurus_websearch.service import build_module


def _make_client(results: list[SearchResult]) -> SearXNGClient:
    client = AsyncMock(spec=SearXNGClient)
    client.search = AsyncMock(return_value=results)
    return client  # type: ignore[return-value]


SAMPLE_RESULTS: list[SearchResult] = [
    SearchResult(title="T1", url="https://t1.com", snippet="S1", engine="google"),
    SearchResult(title="T2", url="https://t2.com", snippet="S2", engine="bing"),
]


async def test_web_search_tool_returns_results() -> None:
    client = _make_client(SAMPLE_RESULTS)
    module = build_module(client)
    _content, structured = await module.mcp.call_tool("web_search", {"query": "hello"})
    assert structured is not None
    result = structured.get("result") or structured
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["title"] == "T1"


async def test_web_search_tool_caps_at_20() -> None:
    client = _make_client(SAMPLE_RESULTS)
    module = build_module(client)
    await module.mcp.call_tool("web_search", {"query": "q", "num_results": 999})
    client.search.assert_called_once_with("q", 20)  # type: ignore[attr-defined]


async def test_web_search_returns_empty_on_exception() -> None:
    client = AsyncMock(spec=SearXNGClient)
    client.search = AsyncMock(side_effect=Exception("network error"))
    module = build_module(client)  # type: ignore[arg-type]
    _content, structured = await module.mcp.call_tool("web_search", {"query": "q"})
    result = structured.get("result") if structured else []
    assert result == [] or structured == {"result": []}


async def test_manifest_declares_tool_and_ui() -> None:
    client = _make_client([])
    module = build_module(client)
    manifest = await module.manifest()
    tool_names = {t.name for t in manifest.tools}
    assert "web_search" in tool_names
    assert manifest.ui is not None
    assert manifest.ui.status_url == "/status"
    assert manifest.ui.icon == "globe"


async def test_manifest_tool_describes_query_param() -> None:
    client = _make_client([])
    module = build_module(client)
    manifest = await module.manifest()
    (tool,) = [t for t in manifest.tools if t.name == "web_search"]
    assert "query" in tool.input_schema.get("properties", {})


async def test_default_max_results_respected() -> None:
    """max_results passed to build_module becomes the tool default."""
    client = _make_client(SAMPLE_RESULTS)
    module = build_module(client, max_results=3)
    await module.mcp.call_tool("web_search", {"query": "q"})
    client.search.assert_called_once_with("q", 3)  # type: ignore[attr-defined]


async def test_custom_num_results_overrides_default() -> None:
    client = _make_client(SAMPLE_RESULTS)
    module = build_module(client, max_results=5)
    await module.mcp.call_tool("web_search", {"query": "q", "num_results": 2})
    client.search.assert_called_once_with("q", 2)  # type: ignore[attr-defined]
