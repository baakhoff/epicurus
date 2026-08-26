"""Tests for the live model catalog (#269): the HTML parser and the refresh lifecycle.

Two layers, neither of which touches the network:

* a synthetic page built by :func:`_model_block`, which renders the library's **current**
  markup and lets each behaviour be exercised against a controlled input; and
* ``tests/fixtures/ollama-library.html`` — a trimmed *verbatim* capture of the live index
  (2026-07-25), which pins the real markup so the next upstream redesign fails here rather
  than silently parsing to ``[]`` on the box for weeks (#710).

``ModelCatalog`` is driven with an injected fetcher + clock for deterministic assertions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs
from structlog.types import EventDict

from epicurus_core_app.llm.catalog import (
    KNOWN_TAGS,
    CatalogEntry,
    ModelCatalog,
    parse_library,
)
from epicurus_core_app.llm.variants import TagInfo

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _unconfigured_structlog() -> Iterator[None]:
    """Isolate this file from another test's ``configure_logging()`` call.

    Whichever test boots the real app first pins structlog's global ``wrapper_class`` to the
    process's configured level (``info``), which silently drops every ``log.debug(...)`` —
    including the ones the bounded-logging tests below assert on. Reset to structlog's
    unfiltered defaults per test and restore whatever was configured beforehand.
    """
    was_configured = structlog.is_configured()
    prev_config = structlog.get_config() if was_configured else None
    structlog.reset_defaults()
    yield
    if was_configured and prev_config is not None:
        structlog.configure(**prev_config)
    else:
        structlog.reset_defaults()


def _chip(text: str, background: str) -> str:
    """One badge span. The parser keys on the rounded-badge idiom + the chip's *text*, never
    on ``background`` — the colours are here only to keep the fixture faithful."""
    return (
        f'<span  class="inline-flex items-center rounded-md {background} px-2 py-0.5 '
        f'text-xs font-medium sm:text-[13px]">{text}</span>'
    )


def _model_block(
    name: str,
    *,
    description: str | None,
    sizes: list[str],
    caps: list[str],
    pulls: str,
    cloud_pill: bool = False,
) -> str:
    """Render one library model anchor the way ollama.com/library does today (#710).

    The double space after ``<div``/``<span`` is not a typo — it is exactly what the live
    page emits where the old ``x-test-*`` attributes used to sit.
    """
    title = f'<div  title="{name}" class="flex flex-col">'
    title += (
        '<h2 class="truncate text-xl font-medium"><div class="flex space-x-2 items-center">'
        f'<span class="group-hover:underline truncate">{name}</span></div></h2>'
    )
    if description is not None:
        title += f'<p class="max-w-lg break-words text-neutral-800 text-md">{description}</p>'
    title += "</div>"
    chips = "".join(_chip(c, "bg-indigo-50") for c in caps)
    if cloud_pill:
        # The cloud pill is a chip like any other, told apart by its text, not its colour.
        chips += _chip("cloud", "bg-cyan-50")
    chips += "".join(_chip(s, "bg-[#ddf4ff]") for s in sizes)
    stats = (
        '<p class="my-4 flex space-x-5 text-[13px] font-medium text-neutral-500">'
        '<span class="flex items-center"><svg viewBox="0 0 24 24"><path d="M3 16.5v2.25">'
        f"</path></svg><span >{pulls}</span>"
        '<span class="hidden sm:flex">&nbsp;Pulls</span></span>'
        '<span class="flex items-center"><span >9</span>'
        '<span class="hidden sm:flex">&nbsp;Tags</span></span>'
        '<span class="flex items-center"><span class="hidden sm:flex">Updated&nbsp;</span>'
        "<span >yesterday</span></span></p>"
    )
    return (
        '<li  class="flex items-baseline border-b border-neutral-200 py-6">'
        f'<a href="/library/{name}" class="group w-full space-y-5">{title}'
        f'<div class="flex flex-col space-y-2"><div class="flex flex-wrap space-x-2">'
        f"{chips}</div>{stats}</div></a></li>"
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
    # its capability chips (tools/thinking) still map through (#571).
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
    # A page whose model links are gone parses to nothing rather than to junk — this is the
    # shape the 2026 redesign took (#710), and the empty result is what flags the failure.
    assert parse_library('<li class="flex"><span>7b</span></li>') == []
    # A blank name is skipped, not emitted as a nameless entry.
    assert parse_library('<a href="/library/ " class="group"><span>x</span></a>') == []


def test_parse_ignores_tags_page_links() -> None:
    # A tags page is all ``/library/<family>:<tag>`` links. Fed here by mistake it must parse
    # to nothing (→ a flagged failed refresh), never to a catalog of colon-bearing families.
    document = (
        '<a href="/library/llama3.1:8b" class="group">'
        f"{_chip('8b', 'bg-[#ddf4ff]')}</a>"
        '<a href="/library/llama3.1:70b" class="group">'
        f"{_chip('70b', 'bg-[#ddf4ff]')}</a>"
    )
    assert parse_library(document) == []


def test_parse_ignores_stats_paragraph_as_description() -> None:
    # A model with no blurb <p> must not adopt the pulls/tags/updated line as its text.
    block = _model_block("ghost", description=None, sizes=["7b"], caps=["tools"], pulls="1M")
    entry = _entry_by_id(parse_library(block), "ghost:7b")
    assert entry.description == ""


def test_parse_keeps_a_blurb_that_mentions_the_stats_words() -> None:
    # The stats line is told apart structurally (its labels are whole elements), not by
    # keyword — so a blurb that merely *says* "updated"/"tags"/"pulls" survives. The previous
    # word-boundary guard blanked 7 of the library's 233 families this way, mistral included.
    block = _model_block(
        "mistral",
        description="The 7B model released by Mistral AI, updated to version 0.3.",
        sizes=["7b"],
        caps=["tools"],
        pulls="31.4M",
    )
    entry = _entry_by_id(parse_library(block), "mistral:7b")
    assert entry.description == "The 7B model released by Mistral AI, updated to version 0.3."


def test_parse_reads_moe_and_effective_size_chips() -> None:
    # Sizes are not all "<number>b": mixture-of-experts families publish "8x7b"/"128x17b" and
    # Gemma-3n-style families publish "effective" sizes "e2b"/"e4b". Each is a real pullable
    # ref, so a stricter size pattern would silently drop the entry entirely.
    document = _model_block(
        "mixtral",
        description="A Mixture of Experts model.",
        sizes=["8x7b", "8x22b"],
        caps=["tools"],
        pulls="2.8M",
    ) + _model_block(
        "gemma3n",
        description="Efficient on-device models.",
        sizes=["e2b", "e4b"],
        caps=[],
        pulls="1.9M",
    )
    ids = {e.id for e in parse_library(document)}
    assert {"mixtral:8x7b", "mixtral:8x22b", "gemma3n:e2b", "gemma3n:e4b"} <= ids


def test_parse_ignores_a_capability_outside_the_known_vocabulary() -> None:
    # Upstream ships capabilities we have no tag for ("audio" is live today). An unknown chip
    # must be dropped, never mistaken for a size and expanded into an unpullable entry.
    block = _model_block(
        "gemma4",
        description="Frontier multimodal models.",
        sizes=["12b"],
        caps=["vision", "audio"],
        pulls="19.5M",
    )
    entries = parse_library(block)
    assert {e.id for e in entries} == {"gemma4:12b"}
    assert "audio" not in entries[0].tags
    assert "vision" in entries[0].tags


def test_parse_ranks_comma_grouped_pull_counts() -> None:
    # Counts under 10K are rendered in full with separators ("8,171"). They must rank below a
    # millions-count model rather than parsing to 0 and sorting arbitrarily.
    document = _model_block(
        "tiny-new", description="A brand-new model.", sizes=["7b"], caps=[], pulls="8,171"
    ) + _model_block(
        "popular", description="An established model.", sizes=["7b"], caps=[], pulls="1.2M"
    )
    entries = parse_library(document)
    assert [e.family for e in entries] == ["popular", "tiny-new"]
    assert _entry_by_id(entries, "tiny-new:7b").pulls == "8,171"


# ── the live-markup pin (#710) ────────────────────────────────────────────────
#
# These run against ``fixtures/ollama-library.html`` — twelve blocks kept verbatim from the
# live index. Their job is to fail when upstream restyles the page, which is what the
# ``x-test-*`` selectors did *not* do: they simply parsed nothing, and the box served the
# seed with a repeating warn for weeks. Regenerate the fixture from a fresh capture (same
# families) when it does; never hand-edit it to make a test pass.


def _live_index() -> str:
    return (FIXTURES / "ollama-library.html").read_text(encoding="utf-8")


def test_live_index_parses_every_captured_family() -> None:
    entries = parse_library(_live_index())
    families: list[str] = []
    for entry in entries:
        if entry.family not in families:
            families.append(entry.family)
    # Popularity order, straight off the real page's pull counts (117.7M … 5,650).
    assert families == [
        "llama3.1",
        "deepseek-r1",
        "nomic-embed-text",
        "gemma3",
        "mistral",
        "gemma4",
        "llava",
        "mixtral",
        "glm-5.1",
        "internlm2",
        "laguna-s-2.1",
        "granite4.1-guardian",
    ]
    assert len(entries) == 34  # one per published size, plus one per size-less family


def test_live_index_expands_real_size_labels() -> None:
    ids = {e.id for e in parse_library(_live_index())}
    # Plain sizes, sub-1B sizes, mixture-of-experts sizes, and "effective" sizes alike.
    assert {"llama3.1:405b", "gemma3:270m", "internlm2:1m"} <= ids
    assert {"mixtral:8x7b", "mixtral:8x22b"} <= ids
    assert {"gemma4:e2b", "gemma4:e4b"} <= ids
    # A size-less family stays a single bare, pullable ref.
    assert "nomic-embed-text" in ids and "nomic-embed-text:" not in ids


def test_live_index_reads_descriptions_pulls_and_capabilities() -> None:
    entries = parse_library(_live_index())
    llama = _entry_by_id(entries, "llama3.1:8b")
    assert llama.pulls == "117.7M"
    assert llama.description.startswith("Llama 3.1 is a new state-of-the-art model from Meta")
    assert llama.tags == ["general", "tools"]
    # A blurb containing the word "updated" is kept (the guard is structural, not keyword).
    assert "updated to version 0.3" in _entry_by_id(entries, "mistral:7b").description
    # A non-ASCII blurb survives entity-unescaping intact.
    assert _entry_by_id(entries, "llava:7b").description.strip()
    # Counts under 10K carry separators and still rank (they sort last here, not first).
    assert _entry_by_id(entries, "laguna-s-2.1").pulls == "8,171"


def test_live_index_vision_models_not_mistagged_as_code() -> None:
    # llava's description contains "vision encoder", which contains "encoder".
    # Without word boundaries, the code pattern cod(?:e|er|ing) would match "encoder"
    # and incorrectly tag the vision model as "code". Regression test for issue #727.
    entries = parse_library(_live_index())
    llava = _entry_by_id(entries, "llava:7b")
    assert "vision" in llava.tags
    assert "code" not in llava.tags
    assert llava.description.strip()  # description exists and contains "encoder"


def test_live_index_distinguishes_cloud_only_from_hybrid() -> None:
    entries = parse_library(_live_index())
    # A cloud-only family: one bare entry, tagged cloud.
    assert set(_entry_by_id(entries, "glm-5.1").tags) >= {"cloud", "thinking", "tools"}
    # A hybrid carries the cloud pill *and* downloadable sizes; its size rows are ordinary
    # local builds and stay untagged. Its "audio" capability has no tag and is dropped.
    for entry_id in ("gemma4:e2b", "gemma4:12b", "gemma4:31b"):
        tags = _entry_by_id(entries, entry_id).tags
        assert "cloud" not in tags and "audio" not in tags
        assert "vision" in tags
    # An embedding family is tagged embedding and, deliberately, not "general".
    assert _entry_by_id(entries, "nomic-embed-text").tags == ["embedding"]


def test_live_index_tags_stay_within_the_known_vocabulary() -> None:
    known = set(KNOWN_TAGS)
    assert all(set(e.tags) <= known for e in parse_library(_live_index()))


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


# ── bounded failure logging (#710) ────────────────────────────────────────────


def _events(logs: list[EventDict], level: str) -> list[EventDict]:
    return [entry for entry in logs if entry.get("log_level") == level]


async def test_a_failure_streak_warns_once_then_drops_to_debug() -> None:
    catalog = ModelCatalog(
        source_url="http://example/library",
        refresh_seconds=3600,
        seed=_SEED,
        fetch=_raising_fetch,
    )
    with capture_logs() as logs:
        for _ in range(5):
            await catalog.refresh()
    # One warn for the streak — not one per refresh, which is what wrote the same line to the
    # box's log every few minutes for weeks while upstream's markup was broken.
    warnings = _events(logs, "warning")
    assert len(warnings) == 1
    assert warnings[0]["error"] == "network down"
    debugs = _events(logs, "debug")
    assert len(debugs) == 4
    assert debugs[-1]["failures"] == 5


async def test_a_new_error_warns_again() -> None:
    errors = iter(["network down", "network down", "parsed catalog was empty"])

    async def fetch(_url: str) -> str:
        raise RuntimeError(next(errors))

    catalog = ModelCatalog(
        source_url="http://example/library", refresh_seconds=3600, seed=_SEED, fetch=fetch
    )
    with capture_logs() as logs:
        for _ in range(3):
            await catalog.refresh()
    # A *changed* symptom is news even mid-streak, so it warns rather than staying quiet.
    assert [w["error"] for w in _events(logs, "warning")] == [
        "network down",
        "parsed catalog was empty",
    ]


async def test_recovery_reports_the_streak_and_rearms_the_warning() -> None:
    failing = True

    async def fetch(_url: str) -> str:
        if failing:
            raise RuntimeError("network down")
        return FIXTURE

    catalog = ModelCatalog(
        source_url="http://example/library",
        refresh_seconds=3600,
        seed=_SEED,
        fetch=fetch,
        clock=lambda: _FIXED_NOW,
    )
    with capture_logs() as logs:
        for _ in range(3):
            await catalog.refresh()
        failing = False
        assert await catalog.refresh() is True
    # The recovery says how long the (mostly debug-quiet) outage ran — otherwise the one
    # thing an operator never sees is that it ended.
    recovered = [entry for entry in logs if entry.get("recovered_after")]
    assert len(recovered) == 1
    assert recovered[0]["recovered_after"] == 3

    # The streak is closed, so the next failure warns again instead of staying at debug.
    failing = True
    with capture_logs() as logs:
        await catalog.refresh()
    assert len(_events(logs, "warning")) == 1


async def test_a_successful_refresh_logs_no_recovery_field() -> None:
    async def fetch(_url: str) -> str:
        return FIXTURE

    catalog = ModelCatalog(
        source_url="http://example/library",
        refresh_seconds=3600,
        seed=_SEED,
        fetch=fetch,
        clock=lambda: _FIXED_NOW,
    )
    with capture_logs() as logs:
        assert await catalog.refresh() is True
    assert all("recovered_after" not in entry for entry in logs)


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
