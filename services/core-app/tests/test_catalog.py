"""Tests for the live model catalog (#269): the HTML parser and the refresh lifecycle.

The parser is exercised against a fixture modeled on the real Ollama library markup
(the stable ``x-test-*`` anchors), so no test touches the network. ``ModelCatalog`` is
driven with an injected fetcher + clock for deterministic, offline assertions.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime

from epicurus_core_app.llm.catalog import (
    CatalogEntry,
    ModelCatalog,
    parse_library,
)


def _model_block(
    name: str,
    *,
    description: str | None,
    sizes: list[str],
    caps: list[str],
    pulls: str,
) -> str:
    """Render one library ``<li x-test-model>`` block like ollama.com/library does."""
    title = f'<div x-test-model-title title="{name}" class="flex flex-col">'
    title += f"<h2><div><span>{name}</span></div></h2>"
    if description is not None:
        title += f'<p class="max-w-lg">{description}</p>'
    title += "</div>"
    pills = "".join(f"<span x-test-capability>{c}</span>" for c in caps)
    pills += "".join(f"<span x-test-size>{s}</span>" for s in sizes)
    stats = (
        '<p class="my-4 flex"><span class="flex"><svg></svg>'
        f"<span x-test-pull-count>{pulls}</span><span>&nbsp;Pulls</span></span>"
        "<span><span x-test-tag-count>9</span> Tags</span>"
        "<span x-test-updated>yesterday</span></p>"
    )
    return (
        f'<li x-test-model class="flex"><a href="/library/{name}" class="group">'
        f'{title}<div class="flex flex-col">{pills}{stats}</div></a></li>'
    )


# A fixture page: most-pulled first is *not* the source order, so ordering is tested too.
FIXTURE = (
    "<html><body><ul role='list'>"
    + _model_block(
        "gemma3",
        description="Google's Gemma 3 — multimodal with strong multilingual support.",
        sizes=["1b", "4b", "12b"],
        caps=["vision"],
        pulls="38M",
    )
    + _model_block(
        "llama3.1",
        description="Llama 3.1 is a state-of-the-art model from Meta in 8B, 70B and 405B.",
        sizes=["8b", "70b", "405b"],
        caps=["tools"],
        pulls="116.3M",
    )
    + _model_block(
        "qwen2.5-coder",
        description="Alibaba's best open code model — completions and debugging.",
        sizes=["1.5b", "7b"],
        caps=["tools"],
        pulls="10M",
    )
    + _model_block(
        "nomic-embed-text",
        description="Fast, high-quality text embeddings — the go-to for local RAG.",
        sizes=[],
        caps=["embedding"],
        pulls="75.6M",
    )
    + "</ul></body></html>"
)

_FIXED_NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
_SEED = [CatalogEntry(id="seed-model", family="seed-model", params="", description="seed")]


def _entry_by_id(entries: list[CatalogEntry], entry_id: str) -> CatalogEntry:
    return next(e for e in entries if e.id == entry_id)


# ── parse_library ─────────────────────────────────────────────────────────────


def test_parse_expands_one_entry_per_size() -> None:
    entries = parse_library(FIXTURE)
    ids = {e.id for e in entries}
    # llama3.1 → three sizes; nomic (size-less) → a single bare entry.
    assert {"llama3.1:8b", "llama3.1:70b", "llama3.1:405b"} <= ids
    assert "nomic-embed-text" in ids
    assert "nomic-embed-text:" not in ids  # no trailing colon for size-less models


def test_parse_carries_family_params_description_pulls() -> None:
    entry = _entry_by_id(parse_library(FIXTURE), "llama3.1:8b")
    assert entry.family == "llama3.1"
    assert entry.params == "8b"
    assert entry.pulls == "116.3M"
    assert "state-of-the-art" in entry.description
    assert entry.size_gb is None  # the library does not publish on-disk size


def test_parse_orders_by_popularity() -> None:
    # Families appear most-pulled first regardless of source order:
    # llama3.1 (116.3M) > nomic (75.6M) > gemma3 (38M) > qwen-coder (10M).
    families: list[str] = []
    for entry in parse_library(FIXTURE):
        if entry.family not in families:
            families.append(entry.family)
    assert families == ["llama3.1", "nomic-embed-text", "gemma3", "qwen2.5-coder"]


def test_parse_max_models_keeps_most_popular_families() -> None:
    entries = parse_library(FIXTURE, max_models=2)
    assert {e.family for e in entries} == {"llama3.1", "nomic-embed-text"}


def test_parse_derives_tags() -> None:
    entries = parse_library(FIXTURE)
    # embedding capability → "embedding"; embedders are not tagged "general".
    nomic = _entry_by_id(entries, "nomic-embed-text")
    assert nomic.tags == ["embedding"]
    # vision capability + a sub-2B size + "multilingual" in the blurb → all four.
    assert set(_entry_by_id(entries, "gemma3:1b").tags) == {
        "general",
        "vision",
        "small",
        "multilingual",
    }
    # a 12B vision model is not "small".
    assert "small" not in _entry_by_id(entries, "gemma3:12b").tags
    # "coder" in the name → "code"; the 1.5B size is also "small"; the tools capability → "tools".
    assert set(_entry_by_id(entries, "qwen2.5-coder:1.5b").tags) == {
        "general",
        "code",
        "small",
        "tools",
    }
    assert "small" not in _entry_by_id(entries, "qwen2.5-coder:7b").tags
    # a plain tools model is general + tools (the capability is now surfaced, #model-caps).
    assert _entry_by_id(entries, "llama3.1:8b").tags == ["general", "tools"]


def test_parse_tags_only_use_the_known_vocabulary() -> None:
    known = {"general", "code", "multilingual", "vision", "tools", "embedding", "small"}
    assert all(set(e.tags) <= known for e in parse_library(FIXTURE))


def test_parse_empty_or_unrecognised_returns_empty() -> None:
    assert parse_library("") == []
    assert parse_library("<html><body>nothing here</body></html>") == []
    # A block with no name (no href, no title) is skipped, not emitted blank.
    assert parse_library("<li x-test-model><span x-test-size>7b</span></li>") == []


def test_parse_ignores_stats_paragraph_as_description() -> None:
    # A model with no blurb <p> must not adopt the pulls/tags/updated line as its text.
    block = _model_block("ghost", description=None, sizes=["7b"], caps=["tools"], pulls="1M")
    entry = _entry_by_id(parse_library(block), "ghost:7b")
    assert entry.description == ""


# ── ModelCatalog ──────────────────────────────────────────────────────────────


async def test_snapshot_serves_seed_before_first_refresh() -> None:
    catalog = ModelCatalog(
        source_url="http://example/library",
        refresh_seconds=3600,
        seed=_SEED,
        fetch=_raising_fetch,
    )
    snap = await catalog.snapshot()
    assert snap.entries == _SEED
    assert snap.stale is True
    assert snap.updated_at is None
    assert snap.source == "http://example/library"


async def test_refresh_success_swaps_in_parsed_entries() -> None:
    async def fetch(_url: str) -> str:
        return FIXTURE

    catalog = ModelCatalog(
        source_url="http://example/library",
        refresh_seconds=3600,
        seed=_SEED,
        fetch=fetch,
        clock=lambda: _FIXED_NOW,
    )
    assert await catalog.refresh() is True
    snap = await catalog.snapshot()
    assert snap.stale is False
    assert snap.updated_at == _FIXED_NOW
    assert snap.entries[0].family == "llama3.1"  # most-pulled first
    assert snap.entries == parse_library(FIXTURE)


async def test_refresh_failure_keeps_last_good_and_flags_stale() -> None:
    catalog = ModelCatalog(
        source_url="http://example/library",
        refresh_seconds=3600,
        seed=_SEED,
        fetch=_raising_fetch,
    )
    assert await catalog.refresh() is False
    snap = await catalog.snapshot()
    assert snap.entries == _SEED  # seed retained
    assert snap.stale is True


async def test_refresh_empty_parse_is_treated_as_failure() -> None:
    async def fetch(_url: str) -> str:
        return "<html><body>no models</body></html>"

    catalog = ModelCatalog(
        source_url="http://example/library",
        refresh_seconds=3600,
        seed=_SEED,
        fetch=fetch,
    )
    assert await catalog.refresh() is False
    assert (await catalog.snapshot()).entries == _SEED


async def test_disabled_catalog_never_fetches() -> None:
    calls = 0

    async def fetch(_url: str) -> str:
        nonlocal calls
        calls += 1
        return FIXTURE

    catalog = ModelCatalog(
        source_url="http://example/library",
        refresh_seconds=3600,
        enabled=False,
        seed=_SEED,
        fetch=fetch,
    )
    assert await catalog.refresh() is False
    await catalog.run_periodic()  # returns immediately, no loop
    assert calls == 0
    assert (await catalog.snapshot()).entries == _SEED


async def test_run_periodic_refreshes_then_cancels_cleanly() -> None:
    done = asyncio.Event()

    async def fetch(_url: str) -> str:
        done.set()
        return FIXTURE

    catalog = ModelCatalog(
        source_url="http://example/library",
        refresh_seconds=3600,  # long; we cancel after the first pass
        seed=_SEED,
        fetch=fetch,
        clock=lambda: _FIXED_NOW,
    )
    task = asyncio.create_task(catalog.run_periodic())
    await asyncio.wait_for(done.wait(), timeout=2)
    await asyncio.sleep(0)  # let refresh() finish swapping the snapshot in
    assert (await catalog.snapshot()).stale is False

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert task.done()


async def _raising_fetch(_url: str) -> str:
    raise RuntimeError("network down")
