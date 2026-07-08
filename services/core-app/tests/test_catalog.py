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
from epicurus_core_app.llm.variants import TagInfo


def _model_block(
    name: str,
    *,
    description: str | None,
    sizes: list[str],
    caps: list[str],
    pulls: str,
    cloud_pill: bool = False,
) -> str:
    """Render one library ``<li x-test-model>`` block like ollama.com/library does."""
    title = f'<div x-test-model-title title="{name}" class="flex flex-col">'
    title += f"<h2><div><span>{name}</span></div></h2>"
    if description is not None:
        title += f'<p class="max-w-lg">{description}</p>'
    title += "</div>"
    pills = "".join(f"<span x-test-capability>{c}</span>" for c in caps)
    if cloud_pill:
        # The library's cloud pill carries *no* x-test-capability hook (verified 2026-07-09):
        # it's a plain, differently-styled span. The parser must still catch it.
        pills += '<span class="inline-flex items-center rounded-md bg-cyan-50">cloud</span>'
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
# gemma3 is a **hybrid** (downloadable sizes + a cloud pill, like the real gemma3/gpt-oss);
# deepseek-v4-flash is **cloud-only** (a pill and no sizes at all).
FIXTURE = (
    "<html><body><ul role='list'>"
    + _model_block(
        "gemma3",
        description="Google's Gemma 3 — multimodal with strong multilingual support.",
        sizes=["1b", "4b", "12b"],
        caps=["vision"],
        pulls="38M",
        cloud_pill=True,
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
    + _model_block(
        "deepseek-v4-flash",
        description="A preview of the DeepSeek-V4 series for efficient reasoning.",
        sizes=[],
        caps=["tools", "thinking"],
        pulls="500K",
        cloud_pill=True,
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
    # llama3.1 (116.3M) > nomic (75.6M) > gemma3 (38M) > qwen-coder (10M) > deepseek (500K).
    families: list[str] = []
    for entry in parse_library(FIXTURE):
        if entry.family not in families:
            families.append(entry.family)
    assert families == [
        "llama3.1",
        "nomic-embed-text",
        "gemma3",
        "qwen2.5-coder",
        "deepseek-v4-flash",
    ]


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


def test_parse_derives_cloud_and_thinking() -> None:
    entries = parse_library(FIXTURE)
    # A cloud-only family (a cloud pill, no sizes) becomes one bare entry tagged "cloud";
    # its x-test-capability chips (tools/thinking) still map through (#571).
    cloud_only = _entry_by_id(entries, "deepseek-v4-flash")
    assert cloud_only.params == ""
    assert set(cloud_only.tags) == {"general", "tools", "thinking", "cloud"}
    # A hybrid family (cloud pill *and* downloadable sizes, like the real gemma3/gpt-oss)
    # keeps its size-expanded rows untagged — they're ordinary local builds.
    for entry_id in ("gemma3:1b", "gemma3:4b", "gemma3:12b"):
        assert "cloud" not in _entry_by_id(entries, entry_id).tags


def test_parse_tags_only_use_the_known_vocabulary() -> None:
    known = {
        "general",
        "code",
        "multilingual",
        "vision",
        "tools",
        "thinking",
        "embedding",
        "small",
        "cloud",
    }
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


# ── GB size fill (#571) ───────────────────────────────────────────────────────

# Tags-page rows per family, as the variant lookup's ``family_tags`` would serve them.
_FAMILY_TAGS: dict[str, list[TagInfo]] = {
    "llama3.1": [
        TagInfo("latest", 4.9),
        TagInfo("8b", 4.9),
        TagInfo("8b-instruct-q8_0", 8.5),
        TagInfo("70b", 43.0),
        TagInfo("405b", 243.0),
    ],
    "nomic-embed-text": [TagInfo("latest", 0.274), TagInfo("v1.5", 0.274)],
    "deepseek-v4-flash": [TagInfo("cloud", None)],
}


def _fixture_catalog(
    source: dict[str, list[TagInfo]] | None = None,
    calls: list[str] | None = None,
) -> ModelCatalog:
    """A catalog over FIXTURE with an injected tags-page source that records its calls."""

    async def fetch(_url: str) -> str:
        return FIXTURE

    async def tag_source(family: str) -> list[TagInfo]:
        if calls is not None:
            calls.append(family)
        return (source if source is not None else _FAMILY_TAGS).get(family, [])

    return ModelCatalog(
        source_url="http://example/library",
        refresh_seconds=3600,
        seed=_SEED,
        fetch=fetch,
        clock=lambda: _FIXED_NOW,
        tag_source=tag_source,
    )


async def test_enrich_family_applies_default_build_sizes() -> None:
    catalog = _fixture_catalog()
    await catalog.refresh()
    assert await catalog.enrich_family("llama3.1") is True
    entries = (await catalog.snapshot()).entries
    # Each sized row takes its bare tag's size — the default build, not a quant variant.
    assert _entry_by_id(entries, "llama3.1:8b").size_gb == 4.9
    assert _entry_by_id(entries, "llama3.1:70b").size_gb == 43.0
    assert _entry_by_id(entries, "llama3.1:405b").size_gb == 243.0


async def test_enrich_family_sizes_a_sizeless_family_from_latest() -> None:
    catalog = _fixture_catalog()
    await catalog.refresh()
    assert await catalog.enrich_family("nomic-embed-text") is True
    entries = (await catalog.snapshot()).entries
    # A size-less downloadable family (no params chip) still gets a GB label — from the
    # ``latest`` alias, its default pull.
    assert _entry_by_id(entries, "nomic-embed-text").size_gb == 0.274


async def test_enrich_family_never_sizes_a_cloud_row() -> None:
    # Even a (hypothetical) sized tag on a cloud-only family must not give the cloud row a
    # GB label — no local weights, no size, by design.
    catalog = _fixture_catalog(source={"deepseek-v4-flash": [TagInfo("weird", 9.9)]})
    await catalog.refresh()
    assert await catalog.enrich_family("deepseek-v4-flash") is False
    assert _entry_by_id((await catalog.snapshot()).entries, "deepseek-v4-flash").size_gb is None


async def test_enrich_family_failure_leaves_entries_untouched() -> None:
    async def broken(_family: str) -> list[TagInfo]:
        raise RuntimeError("offline")

    async def fetch(_url: str) -> str:
        return FIXTURE

    catalog = ModelCatalog(
        source_url="http://example/library",
        refresh_seconds=3600,
        seed=_SEED,
        fetch=fetch,
        tag_source=broken,
    )
    await catalog.refresh()
    before = (await catalog.snapshot()).entries
    assert await catalog.enrich_family("llama3.1") is False
    assert (await catalog.snapshot()).entries == before


async def test_enrich_is_a_noop_without_a_tag_source() -> None:
    async def fetch(_url: str) -> str:
        return FIXTURE

    catalog = ModelCatalog(
        source_url="http://example/library", refresh_seconds=3600, seed=_SEED, fetch=fetch
    )
    await catalog.refresh()
    assert await catalog.enrich_family("llama3.1") is False


async def test_refresh_carries_enriched_sizes_across_swaps() -> None:
    catalog = _fixture_catalog()
    await catalog.refresh()
    await catalog.enrich_family("llama3.1")
    # The next refresh re-parses the index (which has no sizes); the enriched values must
    # survive the swap instead of blanking until the fill reaches the family again.
    assert await catalog.refresh() is True
    entries = (await catalog.snapshot()).entries
    assert _entry_by_id(entries, "llama3.1:8b").size_gb == 4.9
    assert _entry_by_id(entries, "llama3.1:70b").size_gb == 43.0


async def test_size_fill_walks_most_popular_first_and_visits_each_family_once() -> None:
    calls: list[str] = []
    catalog = _fixture_catalog(calls=calls)
    await catalog.refresh()
    # Drive the fill deterministically, one step at a time (run_size_fill just paces these).
    for _ in range(6):
        await catalog.fill_step()
    # Most-popular first; the cloud-only family is excluded by design; gemma3 and
    # qwen2.5-coder yield nothing (no tags served) but are attempted exactly once.
    assert calls == ["llama3.1", "nomic-embed-text", "gemma3", "qwen2.5-coder"]
    entries = (await catalog.snapshot()).entries
    assert _entry_by_id(entries, "llama3.1:8b").size_gb == 4.9
    assert _entry_by_id(entries, "nomic-embed-text").size_gb == 0.274


async def test_size_fill_restarts_only_for_still_missing_families_after_a_refresh() -> None:
    calls: list[str] = []
    catalog = _fixture_catalog(calls=calls)
    await catalog.refresh()
    for _ in range(5):
        await catalog.fill_step()
    calls.clear()
    # A new refresh starts a new pass — but families whose sizes were carried across the
    # swap are no longer candidates; only the still-missing ones are retried.
    await catalog.refresh()
    for _ in range(4):
        await catalog.fill_step()
    assert calls == ["gemma3", "qwen2.5-coder"]


async def test_run_size_fill_returns_immediately_when_unwired_or_disabled() -> None:
    async def fetch(_url: str) -> str:
        return FIXTURE

    async def source(_family: str) -> list[TagInfo]:
        return []

    # No tag source wired → no loop (this would hang the test if it looped).
    await ModelCatalog(
        source_url="http://example/library", refresh_seconds=3600, seed=_SEED, fetch=fetch
    ).run_size_fill()
    # Catalog disabled (air-gapped) → no loop, no fetches.
    await ModelCatalog(
        source_url="http://example/library",
        refresh_seconds=3600,
        enabled=False,
        seed=_SEED,
        fetch=fetch,
        tag_source=source,
    ).run_size_fill()
    # Fill rate of 0 → explicitly disabled.
    await ModelCatalog(
        source_url="http://example/library",
        refresh_seconds=3600,
        seed=_SEED,
        fetch=fetch,
        tag_source=source,
        size_fill_seconds=0,
    ).run_size_fill()


async def _raising_fetch(_url: str) -> str:
    raise RuntimeError("network down")
