"""The OpenRouter backend and stored-settings delivery for ``web_search`` (#984).

The SearXNG client and the platform client are both mocked: these tests pin which one a call
reaches for each stored choice, and that every outcome of an OpenRouter-backed search keeps the
module's contract — chips and hover-cards on results, a genuine empty, a degraded note, a plain
"no key" message that is not an empty result, and a raised error for a provider failure.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from mcp.types import ContentBlock, TextContent

from epicurus_core import PlatformClient, PlatformError, WebSearchResult
from epicurus_core.contracts import ToolEnvelope
from epicurus_core.module import ToolError
from epicurus_websearch.config import EffectiveConfig, resolve, static_source
from epicurus_websearch.refs import decode_ref
from epicurus_websearch.searxng import SearchOutcome, SearchResult, SearXNGClient
from epicurus_websearch.service import (
    DEGRADED_NO_SEARCH_MESSAGE,
    NO_KEY_MESSAGE,
    BackendStatus,
    build_module,
)


def _searxng(results: list[SearchResult] | None = None) -> Any:
    client = AsyncMock(spec=SearXNGClient)
    client.search = AsyncMock(return_value=SearchOutcome(results=results or []))
    return client


def _platform(*, result: WebSearchResult | None = None, error: Exception | None = None) -> Any:
    platform = AsyncMock(spec=PlatformClient)
    if error is not None:
        platform.web_search = AsyncMock(side_effect=error)
    else:
        platform.web_search = AsyncMock(
            return_value=result or WebSearchResult(backend="openrouter", model="m")
        )
    return platform


def _envelope(content: list[ContentBlock]) -> ToolEnvelope:
    item = content[0]
    assert isinstance(item, TextContent)
    return ToolEnvelope.model_validate_json(item.text)


OPENROUTER = static_source(EffectiveConfig(backend="openrouter", max_results=4))


def _hits(*urls: str) -> WebSearchResult:
    return WebSearchResult.model_validate(
        {
            "results": [
                {"title": f"T{i}", "url": url, "snippet": f"S{i}", "engine": "OpenRouter"}
                for i, url in enumerate(urls)
            ],
            "searched": True,
            "backend": "openrouter",
            "model": "openai/gpt-4.1-nano",
        }
    )


# ── routing by the stored choice ─────────────────────────────────────────────────


async def test_openrouter_choice_goes_through_the_core_not_searxng() -> None:
    searxng, platform = _searxng(), _platform(result=_hits("https://a.example"))
    module = build_module(searxng, config=OPENROUTER, platform=platform)

    await module.call_tool("web_search", {"query": "tides"})

    platform.web_search.assert_awaited_once_with("tides", max_results=4)
    searxng.search.assert_not_called()


async def test_searxng_choice_never_touches_the_core_and_uses_stored_engines() -> None:
    searxng, platform = _searxng(), _platform()
    source = static_source(EffectiveConfig(backend="searxng", max_results=7, engines="ddg"))
    module = build_module(searxng, config=source, platform=platform)

    await module.call_tool("web_search", {"query": "q"})

    searxng.search.assert_awaited_once_with("q", 7, engines="ddg")
    platform.web_search.assert_not_called()


async def test_an_explicit_num_results_wins_and_is_capped() -> None:
    platform = _platform(result=_hits("https://a.example"))
    module = build_module(_searxng(), config=OPENROUTER, platform=platform)
    await module.call_tool("web_search", {"query": "q", "num_results": 99})
    platform.web_search.assert_awaited_once_with("q", max_results=20)


async def test_the_config_is_read_on_every_call_so_a_change_applies_without_restart() -> None:
    choices = iter(["searxng", "openrouter"])

    async def source() -> EffectiveConfig:
        return EffectiveConfig(backend=next(choices))  # type: ignore[arg-type]

    searxng, platform = _searxng(), _platform(result=_hits("https://a.example"))
    module = build_module(searxng, config=source, platform=platform)
    await module.call_tool("web_search", {"query": "q"})
    await module.call_tool("web_search", {"query": "q"})
    assert searxng.search.await_count == 1
    assert platform.web_search.await_count == 1


# ── the three outcomes, and the fourth that is not one ───────────────────────────


async def test_results_carry_chips_whose_hover_card_names_openrouter() -> None:
    platform = _platform(
        result=_hits("https://a.example/x", "https://a.example/x/", "https://b.example")
    )
    status = BackendStatus()
    module = build_module(_searxng(), config=OPENROUTER, platform=platform, status=status)

    content, _ = await module.call_tool("web_search", {"query": "q"})
    envelope = _envelope(content)

    # The same page twice (trailing slash) collapses to one result, as SearXNG's do.
    assert [r.title for r in envelope.entity_refs] == ["T0", "T2"]
    assert all(r.module == "websearch" and r.kind == "result" for r in envelope.entity_refs)
    decoded = decode_ref(envelope.entity_refs[0].ref_id)
    assert decoded["engine"] == "OpenRouter"
    assert decoded["url"] == "https://a.example/x"
    assert "(via OpenRouter)" in envelope.text
    assert status.openrouter_last_result == "results"


async def test_a_search_that_matched_nothing_is_a_plain_empty() -> None:
    result = WebSearchResult(backend="openrouter", model="m", searched=True)
    status = BackendStatus()
    module = build_module(
        _searxng(), config=OPENROUTER, platform=_platform(result=result), status=status
    )
    content, _ = await module.call_tool("web_search", {"query": "q"})
    assert _envelope(content).text == "No web results found."
    assert status.openrouter_last_result == "no results"


async def test_no_search_run_is_degraded_not_empty() -> None:
    result = WebSearchResult(backend="openrouter", model="m", searched=False)
    status = BackendStatus()
    module = build_module(
        _searxng(), config=OPENROUTER, platform=_platform(result=result), status=status
    )
    content, _ = await module.call_tool("web_search", {"query": "q"})
    envelope = _envelope(content)
    assert envelope.text == DEGRADED_NO_SEARCH_MESSAGE
    assert "not confirmed empty" in envelope.text
    assert status.openrouter_last_result == "no search ran"


async def test_a_missing_key_says_so_plainly_and_does_not_fall_back() -> None:
    searxng = _searxng([SearchResult(title="x", url="https://x.example", snippet="", engine="g")])
    error = PlatformError(409, "openrouter_key_missing", "No OpenRouter API key is stored.")
    status = BackendStatus()
    module = build_module(
        searxng, config=OPENROUTER, platform=_platform(error=error), status=status
    )

    content, _ = await module.call_tool("web_search", {"query": "q"})
    envelope = _envelope(content)

    assert envelope.text == NO_KEY_MESSAGE
    assert "not an empty result" in envelope.text
    assert "Models page" in envelope.text
    assert envelope.entity_refs == []
    # No silent switch to the other backend (#984: failover is out of scope).
    searxng.search.assert_not_called()
    assert status.openrouter_last_result == "no OpenRouter key stored"


async def test_a_provider_failure_raises_through_the_tool_error_seam() -> None:
    error = PlatformError(502, "provider_error", "OpenRouter web search failed (402): credits")
    status = BackendStatus()
    module = build_module(
        _searxng(), config=OPENROUTER, platform=_platform(error=error), status=status
    )
    with pytest.raises(ToolError, match=r"OpenRouter web search failed \(402\): credits"):
        await module.call_tool("web_search", {"query": "q"})
    assert status.openrouter_last_result == ("failed: OpenRouter web search failed (402): credits")


async def test_openrouter_without_a_platform_client_says_no_search_ran() -> None:
    module = build_module(_searxng(), config=OPENROUTER, platform=None)
    content, _ = await module.call_tool("web_search", {"query": "q"})
    assert "No search ran" in _envelope(content).text


# ── the manifest ─────────────────────────────────────────────────────────────────


async def test_manifest_declares_the_backend_choice_gated_on_the_openrouter_key() -> None:
    manifest = await build_module(_searxng()).manifest()
    assert manifest.ui is not None
    schema = manifest.ui.config_schema
    assert schema is not None
    backend = schema["properties"]["websearch_backend"]
    assert backend["enum"] == ["searxng", "openrouter"]
    assert backend["default"] == "searxng"
    assert backend["enumRequiresProviderKey"] == [None, "openrouter"]
    assert len(backend["enumLabels"]) == 2
    # The description and summary no longer claim SearXNG is the only way.
    assert "OpenRouter" in manifest.description
    assert "OpenRouter" in manifest.ui.summary
    assert "no API key required" not in manifest.description.lower()


async def test_web_search_num_results_is_optional_in_the_tool_schema() -> None:
    manifest = await build_module(_searxng()).manifest()
    tool = next(t for t in manifest.tools if t.name == "web_search")
    assert tool.input_schema["required"] == ["query"]


# ── stored settings over env defaults ────────────────────────────────────────────


def test_nothing_stored_is_exactly_the_env() -> None:
    assert resolve({}, env_max_results=8, env_engines="bing") == EffectiveConfig(
        backend="searxng", max_results=8, engines="bing"
    )


def test_a_stored_non_default_wins() -> None:
    stored = {
        "websearch_backend": "openrouter",
        "websearch_max_results": 12,
        "websearch_engines": " ddg,brave ",
    }
    assert resolve(stored, env_max_results=8, env_engines="bing") == EffectiveConfig(
        backend="openrouter", max_results=12, engines="ddg,brave"
    )


def test_a_stored_default_does_not_wipe_an_env_value() -> None:
    # The shell's form submits every field; saving it only to change the backend must not
    # replace an env engine list or result count with the form's blank/5 defaults.
    stored = {
        "websearch_backend": "openrouter",
        "websearch_max_results": 5,
        "websearch_engines": "",
    }
    effective = resolve(stored, env_max_results=8, env_engines="bing")
    assert (effective.max_results, effective.engines) == (8, "bing")


@pytest.mark.parametrize(
    "stored",
    [
        {"websearch_backend": "bing"},
        {"websearch_backend": None},
        {"websearch_max_results": "12"},
        {"websearch_max_results": 0},
        {"websearch_max_results": 21},
        {"websearch_max_results": True},
        {"websearch_engines": 3},
    ],
)
def test_malformed_stored_values_fall_back_field_by_field(stored: dict[str, Any]) -> None:
    assert resolve(stored, env_max_results=8, env_engines="bing") == EffectiveConfig(
        backend="searxng", max_results=8, engines="bing"
    )


# ── the running app: delivery through the core, and /status ──────────────────────


async def _true() -> bool:
    return True


def test_the_app_reads_the_stored_choice_through_the_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from epicurus_websearch.app import create_app

    asked: list[str | None] = []

    async def stored(self: PlatformClient) -> dict[str, Any]:
        asked.append(self._module)
        return {"websearch_backend": "openrouter"}

    monkeypatch.setattr(PlatformClient, "get_module_config", stored)
    monkeypatch.setattr(SearXNGClient, "health_check", lambda self: _true())
    body = TestClient(create_app()).get("/status").json()

    assert asked == ["websearch"]
    assert body["backend"] == "openrouter"
    assert body["openrouter_last_result"] is None
    # SearXNG's own fields stay correct whichever backend is chosen.
    assert body["searxng_healthy"] is True
    assert body["degraded"] is False
    assert all(isinstance(v, (str, bool, int, float, type(None))) for v in body.values())


def test_status_reads_searxng_when_the_core_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from epicurus_websearch.app import create_app

    async def down(self: PlatformClient) -> dict[str, Any]:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(PlatformClient, "get_module_config", down)
    monkeypatch.setattr(SearXNGClient, "health_check", lambda self: _true())
    body = TestClient(create_app()).get("/status").json()
    assert body["backend"] == "searxng"
