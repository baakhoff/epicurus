"""Unit tests for the websearch MCP module surface."""

from __future__ import annotations

from unittest.mock import AsyncMock

from mcp.types import ContentBlock, TextContent

from epicurus_core.contracts import ToolEnvelope
from epicurus_websearch.ingest import IngestResult, LinkIngestor
from epicurus_websearch.refs import decode_source_ref
from epicurus_websearch.searxng import SearchResult, SearXNGClient
from epicurus_websearch.service import build_module


def _make_client(results: list[SearchResult]) -> SearXNGClient:
    client = AsyncMock(spec=SearXNGClient)
    client.search = AsyncMock(return_value=results)
    return client


def _text_of(item: ContentBlock) -> str:
    assert isinstance(item, TextContent)
    return item.text


def _parse_envelope(content: list[ContentBlock]) -> ToolEnvelope:
    """Extract the ToolEnvelope from the first TextContent item in a call_tool result."""
    return ToolEnvelope.model_validate_json(_text_of(content[0]))


SAMPLE_RESULTS: list[SearchResult] = [
    SearchResult(title="T1", url="https://t1.com", snippet="S1", engine="google"),
    SearchResult(title="T2", url="https://t2.com", snippet="S2", engine="bing"),
]


async def test_web_search_returns_entity_refs() -> None:
    client = _make_client(SAMPLE_RESULTS)
    module = build_module(client)
    content, _ = await module.call_tool("web_search", {"query": "hello"})
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
    content, _ = await module.call_tool("web_search", {"query": "hello"})
    envelope = _parse_envelope(content)
    assert "T1" in envelope.text
    assert "https://t1.com" in envelope.text
    assert "T2" in envelope.text
    assert "https://t2.com" in envelope.text


async def test_web_search_tool_caps_at_20() -> None:
    client = _make_client(SAMPLE_RESULTS)
    module = build_module(client)
    await module.call_tool("web_search", {"query": "q", "num_results": 999})
    client.search.assert_called_once_with("q", 20)  # type: ignore[attr-defined]


async def test_web_search_returns_no_refs_on_exception() -> None:
    client = AsyncMock(spec=SearXNGClient)
    client.search = AsyncMock(side_effect=Exception("network error"))
    module = build_module(client)
    content, _ = await module.call_tool("web_search", {"query": "q"})
    envelope = _parse_envelope(content)
    assert envelope.entity_refs == []
    assert "No web results" in envelope.text


async def test_web_search_empty_results_returns_no_refs() -> None:
    client = _make_client([])
    module = build_module(client)
    content, _ = await module.call_tool("web_search", {"query": "q"})
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
    content, _ = await module.call_tool("web_search", {"query": "q"})
    envelope = _parse_envelope(content)
    assert len(envelope.entity_refs) == 2
    assert envelope.entity_refs[0].title == "A"  # first occurrence kept


async def test_web_search_two_calls_same_result_produce_same_ref_id() -> None:
    """The core's cross-call `_RefCollector` dedupes on `ref_id` — verify determinism."""
    client = _make_client(SAMPLE_RESULTS)
    module = build_module(client)
    content_a, _ = await module.call_tool("web_search", {"query": "hello"})
    content_b, _ = await module.call_tool("web_search", {"query": "hello again"})
    ref_ids_a = {r.ref_id for r in _parse_envelope(content_a).entity_refs}
    ref_ids_b = {r.ref_id for r in _parse_envelope(content_b).entity_refs}
    assert ref_ids_a == ref_ids_b


async def test_manifest_declares_tool_ui_and_resolver() -> None:
    client = _make_client([])
    module = build_module(client)
    manifest = await module.manifest()
    tool_names = {t.name for t in manifest.tools}
    assert "web_search" in tool_names
    assert "link_ingest" in tool_names
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
    await module.call_tool("web_search", {"query": "q"})
    client.search.assert_called_once_with("q", 3)  # type: ignore[attr-defined]


async def test_custom_num_results_overrides_default() -> None:
    client = _make_client(SAMPLE_RESULTS)
    module = build_module(client, max_results=5)
    await module.call_tool("web_search", {"query": "q", "num_results": 2})
    client.search.assert_called_once_with("q", 2)  # type: ignore[attr-defined]


# ── link_ingest (#739) ─────────────────────────────────────────────────────────────────


def _stub_ingestor(result: IngestResult) -> LinkIngestor:
    """A LinkIngestor that returns *result* — the real one is covered in test_ingest.py."""
    ingestor = AsyncMock(spec=LinkIngestor)
    ingestor.ingest = AsyncMock(return_value=result)
    return ingestor


ARTICLE_RESULT = IngestResult(
    kind="article",
    url="https://example.com/a/tidal",
    title="Tidal turbines feed a village",
    site="The Coastal Review",
    author="Marit Halvorsen",
    published="2025-11-14",
    text="A five-turbine array ran a village of 340 through the winter.",
    retrieved_at="2026-08-15",
)


async def test_link_ingest_returns_the_extract_in_the_envelope_text() -> None:
    module = build_module(_make_client([]), ingestor=_stub_ingestor(ARTICLE_RESULT))
    content, _ = await module.call_tool("link_ingest", {"url": "https://example.com/a/tidal"})
    text = _parse_envelope(content).text
    assert "Tidal turbines feed a village" in text
    assert "Marit Halvorsen" in text
    assert "source: https://example.com/a/tidal" in text
    assert "A five-turbine array" in text


async def test_link_ingest_emits_one_source_entity_ref() -> None:
    module = build_module(_make_client([]), ingestor=_stub_ingestor(ARTICLE_RESULT))
    content, _ = await module.call_tool("link_ingest", {"url": "https://example.com/a/tidal"})
    (ref,) = _parse_envelope(content).entity_refs
    assert ref.module == "websearch"
    assert ref.kind == "source"
    assert ref.title == "Tidal turbines feed a village"
    decoded = decode_source_ref(ref.ref_id)
    assert decoded["url"] == "https://example.com/a/tidal"
    assert decoded["kind"] == "article"
    assert decoded["site"] == "The Coastal Review"


async def test_link_ingest_surfaces_an_unreachable_link_as_a_normal_result() -> None:
    """#739's honesty rule: a refusal is a well-formed answer, never a failed turn."""
    unreachable = IngestResult(
        kind="unreachable",
        url="http://127.0.0.1:8080/health",
        ok=False,
        notes=["refused: 127.0.0.1 is a private or reserved address"],
    )
    module = build_module(_make_client([]), ingestor=_stub_ingestor(unreachable))
    content, _ = await module.call_tool("link_ingest", {"url": "http://127.0.0.1:8080/health"})
    envelope = _parse_envelope(content)
    assert "private or reserved address" in envelope.text
    assert len(envelope.entity_refs) == 1


async def test_link_ingest_without_an_ingestor_says_so_rather_than_failing() -> None:
    module = build_module(_make_client([]))
    content, _ = await module.call_tool("link_ingest", {"url": "https://example.com/"})
    envelope = _parse_envelope(content)
    assert "not configured" in envelope.text
    assert envelope.entity_refs == []


async def test_manifest_link_ingest_describes_its_url_param() -> None:
    module = build_module(_make_client([]))
    manifest = await module.manifest()
    (tool,) = [t for t in manifest.tools if t.name == "link_ingest"]
    assert "url" in tool.input_schema.get("properties", {})


async def test_manifest_link_ingest_description_steers_and_states_the_limits() -> None:
    """The docstring is the agent's primary lever (McpHost.discover reads it) — pin the policy.

    Anchors on the load-bearing phrases so the prose can be rewritten, but a rewrite cannot
    quietly drop the reach-for-it cue, the honesty rules, or the hand-off to knowledge.
    """
    module = build_module(_make_client([]))
    manifest = await module.manifest()
    (tool,) = [t for t in manifest.tools if t.name == "link_ingest"]
    description = (tool.description or "").lower()
    assert "reach for this" in description
    assert "snippet is not the page" in description  # read it before summarising
    assert "never signs in" in description  # no login walls, no credentials
    assert "knowledge_propose_edit" in description  # the agent's next step, not ours
    assert "not transcribed yet" in description  # ASR is out of scope, stated to the model


async def test_manifest_version_matches_the_packaged_version() -> None:
    """The hardcoded manifest version drifted from ``pyproject.toml`` once already (0.2.1 vs
    0.2.2) — read the declared version rather than the installed metadata, which a stale
    editable install can lie about."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    module = build_module(_make_client([]))
    manifest = await module.manifest()
    assert manifest.version == declared
