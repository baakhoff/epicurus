"""Tests for the on-demand quant-variant lookup (#330) and its tags-page size parse (#571).

The tag parser is pure and exercised directly; ``VariantLookup`` is driven with an injected
text fetcher (the library tags HTML page) and clock so no test touches the network. The page
fixture mirrors the real markup's key property: every tag row is rendered **twice** (a mobile
and a desktop layout), both carrying the same size string.
"""

from __future__ import annotations

import httpx
import pytest

from epicurus_core_app.llm.variants import (
    TagInfo,
    VariantLookup,
    parse_variant_tags,
    size_text_to_gb,
    tags_from_page,
)

# A realistic library tag list spanning two sizes, several quants, and the "latest" alias,
# each with the size string its tags-page row shows (None = a cloud tag with no weights).
LLAMA_ROWS: list[tuple[str, str | None]] = [
    ("latest", "4.9GB"),
    ("8b", "4.9GB"),
    ("8b-instruct-q4_0", "4.7GB"),
    ("8b-instruct-q4_K_M", "4.9GB"),
    ("8b-instruct-q8_0", "8.5GB"),
    ("8b-instruct-fp16", "16GB"),
    ("8b-text-q4_0", "4.7GB"),
    ("70b", "43GB"),
    ("70b-instruct-q4_0", "43GB"),
]


def _infos(rows: list[tuple[str, float | None]]) -> list[TagInfo]:
    return [TagInfo(tag=tag, size_gb=size) for tag, size in rows]


def _row(family: str, tag: str, size: str | None) -> str:
    """One tags-page row the way the library renders it: a mobile layout (the size inline in
    the stats line) and a desktop layout (the size in its own grid cell), same tag twice."""
    stats = f"46e0c10c039e &middot; {size + ' &middot; ' if size else ''}128K context window"
    mobile = (
        f'<a href="/library/{family}:{tag}" class="md:hidden"><span>{family}:{tag}</span>'
        f"<span>{stats}</span></a>"
    )
    size_cell = f'<p class="col-span-2">{size}</p>' if size else ""
    desktop = (
        f'<div class="hidden md:flex"><a href="/library/{family}:{tag}">{family}:{tag}</a>'
        f'{size_cell}<p class="col-span-2">128K</p></div>'
    )
    return f'<div class="group">{mobile}{desktop}</div>'


def _tags_page(family: str, rows: list[tuple[str, str | None]]) -> str:
    """A minimal stand-in for a library tags page: two rendered rows per tag, plus the
    family's own index link and an unrelated model (both must be ignored)."""
    links = [f'<a href="/library/{family}"></a>', '<a href="/library/other-model:1b"></a>']
    body = "".join(_row(family, tag, size) for tag, size in rows)
    return "<html><body>" + "".join(links) + body + "</body></html>"


# ── size_text_to_gb ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4.9GB", 4.9),
        ("133GB", 133.0),
        ("731MB", 0.731),
        ("1.2TB", 1200.0),
        ("46e0c10c039e · 274MB · 128K context window", 0.274),  # first size token wins
        ("128K context window", None),  # a context label is not a size
        ("no size here", None),
        ("", None),
    ],
)
def test_size_text_to_gb(text: str, expected: float | None) -> None:
    result = size_text_to_gb(text)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


# ── parse_variant_tags ────────────────────────────────────────────────────────


def test_parse_filters_to_size_and_parses_quant_and_size() -> None:
    rows: list[tuple[str, float | None]] = [
        ("latest", 4.9),
        ("8b", 4.9),
        ("8b-instruct-q4_K_M", 4.9),
        ("8b-instruct-q8_0", 8.5),
        ("8b-instruct-fp16", 16.0),
        ("8b-text-q4_0", 4.7),
        ("70b", 43.0),
        ("70b-instruct-q4_0", 43.0),
    ]
    variants = parse_variant_tags("llama3.1", "8b", _infos(rows))
    by_tag = {v.tag: v for v in variants}

    # Other sizes and the "latest" alias are excluded; each 8b tag becomes a llama3.1:<tag> ref.
    assert "llama3.1:70b" not in by_tag
    assert "llama3.1:70b-instruct-q4_0" not in by_tag
    assert "llama3.1:latest" not in by_tag
    # The bare size is the default build (no quant token); the rest carry their parsed quant.
    assert by_tag["llama3.1:8b"].quant == ""
    assert by_tag["llama3.1:8b-instruct-q4_K_M"].quant == "q4_K_M"
    assert by_tag["llama3.1:8b-instruct-q8_0"].quant == "q8_0"
    assert by_tag["llama3.1:8b-instruct-fp16"].quant == "fp16"
    # A same-quant-different-build tag is kept distinct (the tag disambiguates).
    assert by_tag["llama3.1:8b-text-q4_0"].quant == "q4_0"
    # Each variant carries the real size its tags-page row showed (#571).
    assert by_tag["llama3.1:8b"].size_gb == 4.9
    assert by_tag["llama3.1:8b-instruct-q8_0"].size_gb == 8.5


def test_parse_sizeless_family_keeps_all_real_tags() -> None:
    variants = parse_variant_tags(
        "nomic-embed-text",
        "",
        _infos([("latest", 0.274), ("v1.5", 0.274), ("v1.5-fp16", 0.274), ("v1.5-q4_0", None)]),
    )
    by_tag = {v.tag: v for v in variants}
    assert "nomic-embed-text:latest" not in by_tag
    assert by_tag["nomic-embed-text:v1.5"].quant == ""
    assert by_tag["nomic-embed-text:v1.5"].size_gb == 0.274
    assert by_tag["nomic-embed-text:v1.5-q4_0"].quant == "q4_0"
    # A row with no size string parses (and serves) as size-unknown, never a guess.
    assert by_tag["nomic-embed-text:v1.5-q4_0"].size_gb is None


# ── tags_from_page ────────────────────────────────────────────────────────────


def test_tags_from_page_dedups_and_scopes_to_family() -> None:
    document = (
        '<a href="/library/qwen3"></a>'  # the family index link — no tag, ignored
        '<a href="/library/qwen3:4b"></a><a href="/library/qwen3:4b"></a>'  # a duplicate link
        '<a href="/library/qwen3:4b-q4_K_M"></a>'
        '<a href="/library/other:1b"></a>'  # a different model — ignored
    )
    assert [t.tag for t in tags_from_page("qwen3", document)] == ["4b", "4b-q4_K_M"]


def test_tags_from_page_extracts_each_rows_size() -> None:
    document = _tags_page("llama3.1", LLAMA_ROWS)
    infos = {t.tag: t.size_gb for t in tags_from_page("llama3.1", document)}
    # One entry per tag despite the double (mobile + desktop) rendering, each with its size.
    assert infos["latest"] == pytest.approx(4.9)
    assert infos["8b"] == pytest.approx(4.9)
    assert infos["8b-instruct-q8_0"] == pytest.approx(8.5)
    assert infos["70b"] == pytest.approx(43.0)
    assert len(infos) == len(LLAMA_ROWS)


def test_tags_from_page_cloud_tag_has_no_size() -> None:
    # A cloud-only family publishes exactly one tag with no size string on its row.
    document = _tags_page("deepseek-v4-flash", [("cloud", None)])
    infos = tags_from_page("deepseek-v4-flash", document)
    assert [t.tag for t in infos] == ["cloud"]
    assert infos[0].size_gb is None


def test_tags_from_page_size_never_bleeds_from_the_next_row() -> None:
    # A size-less row directly above a sized row must not adopt the neighbour's size: the
    # chunk for each tag ends where the next tag's link starts.
    document = _tags_page("mixed", [("cloud", None), ("7b", "4.7GB")])
    infos = {t.tag: t.size_gb for t in tags_from_page("mixed", document)}
    assert infos["cloud"] is None
    assert infos["7b"] == pytest.approx(4.7)


# ── VariantLookup ─────────────────────────────────────────────────────────────


async def test_lookup_returns_variants_with_sizes_from_the_tags_page() -> None:
    seen: dict[str, str] = {}

    async def fetch(url: str) -> str:
        seen["url"] = url
        return _tags_page("llama3.1", LLAMA_ROWS)

    lookup = VariantLookup(library_url="https://lib.example/library/", fetch=fetch)
    resp = await lookup.variants("llama3.1:8b")

    # The tags page (not the registry's non-existent tags/list) is fetched, under the base.
    assert seen["url"] == "https://lib.example/library/llama3.1/tags"
    assert resp.model == "llama3.1:8b"
    by_tag = {v.tag: v for v in resp.variants}
    assert by_tag["llama3.1:8b-instruct-q8_0"].size_gb == pytest.approx(8.5)
    assert by_tag["llama3.1:8b"].size_gb == pytest.approx(4.9)
    assert all(":70b" not in t for t in by_tag)  # filtered to the requested size
    assert all("other-model" not in t for t in by_tag)  # an unrelated model is ignored


async def test_family_tags_caches_per_family_until_ttl() -> None:
    calls = 0
    clock = {"t": 1000.0}

    async def fetch(url: str) -> str:
        nonlocal calls
        calls += 1
        return _tags_page("llama3.1", LLAMA_ROWS)

    lookup = VariantLookup(
        library_url="https://lib.example/library",
        fetch=fetch,
        cache_ttl_seconds=600,
        now=lambda: clock["t"],
    )
    # Two lookups (the UI and the catalog size fill share this path) → one upstream request.
    await lookup.variants("llama3.1:8b")
    tags = await lookup.family_tags("llama3.1")
    assert calls == 1
    assert {t.tag for t in tags} >= {"latest", "8b", "70b"}
    # Past the TTL the page is re-fetched.
    clock["t"] += 601
    await lookup.family_tags("llama3.1")
    assert calls == 2


async def test_family_tags_failure_is_not_cached() -> None:
    calls = 0

    async def fetch(url: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("offline")
        return _tags_page("llama3.1", LLAMA_ROWS)

    lookup = VariantLookup(library_url="https://lib.example/library", fetch=fetch)
    resp = await lookup.variants("llama3.1:8b")
    assert resp.variants == []  # the failure degrades to an empty list…
    resp = await lookup.variants("llama3.1:8b")
    assert len(resp.variants) > 0  # …and the next call retries rather than serving a cached miss
    assert calls == 2


async def test_lookup_is_quiet_and_empty_for_a_non_library_model() -> None:
    # A model not in the public library 404s; that's expected (a local/custom model), so the
    # lookup serves an empty list rather than treating it as an error.
    async def fetch(url: str) -> str:
        raise httpx.HTTPStatusError(
            "not found",
            request=httpx.Request("GET", url),
            response=httpx.Response(404),
        )

    lookup = VariantLookup(library_url="https://lib.example/library", fetch=fetch)
    resp = await lookup.variants("my-local-model:8b")
    assert resp.variants == []


async def test_lookup_is_best_effort_on_failure() -> None:
    async def fetch(url: str) -> str:
        raise RuntimeError("offline")

    lookup = VariantLookup(library_url="https://lib.example/library", fetch=fetch)
    resp = await lookup.variants("llama3.1:8b")
    assert resp.variants == []


async def test_lookup_skips_fetch_for_a_blank_model() -> None:
    called = False

    async def fetch(url: str) -> str:
        nonlocal called
        called = True
        return ""

    lookup = VariantLookup(library_url="https://lib.example/library", fetch=fetch)
    resp = await lookup.variants("")
    assert resp.variants == []
    assert called is False
