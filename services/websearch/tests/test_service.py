"""Unit tests for the websearch MCP module surface."""

from __future__ import annotations

from unittest.mock import AsyncMock

from epicurus_core.contracts import ToolEnvelope
from epicurus_websearch.searxng import SearchResult, SearXNGClient
from epicurus_websearch.service import build_module


def _make_client(results: list[SearchResult]) -> SearXNGClient:
    client = AsyncMock(spec=SearXNGClient)
    client.search = AsyncMock(return_value=results)
    return client  # type: ignore[return-value]


def _parse_envelope(content: list) -> ToolEnvelope:  # type: ignore[type-arg]
    """Extract the ToolEnvelope from the first TextContent item in a call_tool result."""
    text = content[0].text  # type: ignore[attr-defined]
    return ToolEnvelope.model_validate_json(text)


SAMPLE_RESULTS: list[SearchResult] = [
    SearchResult(title="T1", url="https://t1.com", snippet="S1", engine="google"),
    SearchResult(title="T2", url="https://t2.com", snippet="S2", engine="bing"),
]


async def test_web_search_returns_entity_refs() -> None:
    client = _make_client(SAMPLE_RESULTS)
    module = build_module(client)
    content, _ = await module.mcp.call_tool("web_search", {"query": "hello"})
    envelope = _parse_envelope(content)
    assert len(envelope.entity_refs) == 2
    ref = envelope.entity_refs[0]
    assert ref.module == "websearch"
    assert ref.kind == "result"
    assert ref.title == "T1"
    assert ref.summary == "S1"


async def test_web_search_text_mentions_titles_and_urls() -> None:
    client = _make_client(SAMPLE_RESULTS)
    module = build_module(client)
    content, _ = await module.mcp.call_tool("web_search", {"query": "hello"})
    envelope = _parse_envelope(content)
    assert "T1" in envelope.text
    assert "https://t1.com" in envelope.text
    assert "T2" in envelope.text
    assert "https://t2.com" in envelope.text


async def test_web_search_tool_caps_at_20() -> None:
    client = _make_client(SAMPLE_RESULTS)
    module = build_module(client)
    await module.mcp.call_tool("web_search", {"query": "q", "num_results": 999})
    client.search.assert_called_once_with("q", 20)  # type: ignore[attr-defined]


async def test_web_search_returns_no_refs_on_exception() -> None:
    client = AsyncMock(spec=SearXNGClient)
    client.search = AsyncMock(side_effect=Exception("network error"))
    module = build_module(client)  # type: ignore[arg-type]
    content, _ = await module.mcp.call_tool("web_search", {"query": "q"})
    envelope = _parse_envelope(content)
    assert envelope.entity_refs == []
    assert "No web results" in envelope.text


async def test_web_search_empty_results_returns_no_refs() -> None:
    client = _make_client([])
    module = build_module(client)
    content, _ = await module.mcp.call_tool("web_search", {"query": "q"})
    envelope = _parse_envelope(content)
    assert envelope.entity_refs == []
    assert "No web results" in envelope.text


async def test_web_search_dedupes_same_url_within_one_call() -> None:
    results = [
        SearchResult(title="A", url="https://dup.com/page", snippet="S1", engine="google"),
        # Same page, trailing slash + different engine/snippet — still one chip.
        SearchResult(
            title="A (bing copy)", url="https://dup.com/page/", snippet="S2", engine="bing"
        ),
        SearchResult(title="B", url="https://other.com", snippet="S3", engine="google"),
    ]
    client = _make_client(results)
    module = build_module(client)
    content, _ = await module.mcp.call_tool("web_search", {"query": "q"})
    envelope = _parse_envelope(content)
    assert len(envelope.entity_refs) == 2
    assert envelope.entity_refs[0].title == "A"  # first occurrence kept


async def test_web_search_two_calls_same_result_produce_same_ref_id() -> None:
    """The core's cross-call `_RefCollector` dedupes on `ref_id` — verify determinism."""
    client = _make_client(SAMPLE_RESULTS)
    module = build_module(client)
    content_a, _ = await module.mcp.call_tool("web_search", {"query": "hello"})
    content_b, _ = await module.mcp.call_tool("web_search", {"query": "hello again"})
    ref_ids_a = {r.ref_id for r in _parse_envelope(content_a).entity_refs}
    ref_ids_b = {r.ref_id for r in _parse_envelope(content_b).entity_refs}
    assert ref_ids_a == ref_ids_b


async def test_manifest_declares_tool_ui_and_resolver() -> None:
    client = _make_client([])
    module = build_module(client)
    manifest = await module.manifest()
    tool_names = {t.name for t in manifest.tools}
    assert "web_search" in tool_names
    assert manifest.ui is not None
    assert manifest.ui.status_url == "/status"
    assert manifest.ui.icon == "globe"
    assert manifest.resolver is True


async def test_manifest_tool_describes_query_param() -> None:
    client = _make_client([])
    module = build_module(client)
    manifest = await module.manifest()
    (tool,) = [t for t in manifest.tools if t.name == "web_search"]
    assert "query" in tool.input_schema.get("properties", {})


async def test_manifest_tool_description_says_when_to_search() -> None:
    """#703: the description carries when-to-reach-for-it guidance, not just what the tool does."""
    client = _make_client([])
    module = build_module(client)
    manifest = await module.manifest()
    (tool,) = [t for t in manifest.tools if t.name == "web_search"]
    description = (tool.description or "").lower()
    assert "never guess" in description
    assert "operator's own data" in description


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
