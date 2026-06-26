"""Tests for the on-demand quant-variant lookup (#330).

The tag parser is pure and exercised directly; ``VariantLookup`` is driven with an injected
text fetcher (the library tags HTML page) so no test touches the network.
"""

from __future__ import annotations

import httpx

from epicurus_core_app.llm.variants import VariantLookup, parse_variant_tags, tags_from_page

# A realistic library tag list spanning two sizes, several quants, and the "latest" alias.
LLAMA_TAGS = [
    "latest",
    "8b",
    "8b-instruct-q4_0",
    "8b-instruct-q4_K_M",
    "8b-instruct-q8_0",
    "8b-instruct-fp16",
    "8b-text-q4_0",
    "70b",
    "70b-instruct-q4_0",
]


def _tags_page(family: str, tags: list[str]) -> str:
    """A minimal stand-in for a library tags page: one ``/library/<family>:<tag>`` link per tag,
    plus the family's own index link and an unrelated model (both must be ignored)."""
    links = [f'<a href="/library/{family}"></a>', '<a href="/library/other-model:1b"></a>']
    links += [f'<a href="/library/{family}:{tag}"></a>' for tag in tags]
    return "<html><body>" + "".join(links) + "</body></html>"


def test_parse_filters_to_size_and_parses_quant() -> None:
    variants = parse_variant_tags("llama3.1", "8b", LLAMA_TAGS)
    by_tag = {v.tag: v.quant for v in variants}

    # Other sizes and the "latest" alias are excluded; each 8b tag becomes a llama3.1:<tag> ref.
    assert "llama3.1:70b" not in by_tag
    assert "llama3.1:70b-instruct-q4_0" not in by_tag
    assert "llama3.1:latest" not in by_tag
    # The bare size is the default build (no quant token); the rest carry their parsed quant.
    assert by_tag["llama3.1:8b"] == ""
    assert by_tag["llama3.1:8b-instruct-q4_K_M"] == "q4_K_M"
    assert by_tag["llama3.1:8b-instruct-q8_0"] == "q8_0"
    assert by_tag["llama3.1:8b-instruct-fp16"] == "fp16"
    # A same-quant-different-build tag is kept distinct (the tag disambiguates).
    assert by_tag["llama3.1:8b-text-q4_0"] == "q4_0"


def test_parse_sizeless_family_keeps_all_real_tags() -> None:
    variants = parse_variant_tags(
        "nomic-embed-text", "", ["latest", "v1.5", "v1.5-fp16", "v1.5-q4_0"]
    )
    by_tag = {v.tag: v.quant for v in variants}
    assert "nomic-embed-text:latest" not in by_tag
    assert by_tag["nomic-embed-text:v1.5"] == ""
    assert by_tag["nomic-embed-text:v1.5-q4_0"] == "q4_0"


def test_tags_from_page_dedups_and_scopes_to_family() -> None:
    document = (
        '<a href="/library/qwen3"></a>'  # the family index link — no tag, ignored
        '<a href="/library/qwen3:4b"></a><a href="/library/qwen3:4b"></a>'  # a duplicate link
        '<a href="/library/qwen3:4b-q4_K_M"></a>'
        '<a href="/library/other:1b"></a>'  # a different model — ignored
    )
    assert tags_from_page("qwen3", document) == ["4b", "4b-q4_K_M"]


async def test_lookup_returns_variants_from_the_tags_page() -> None:
    seen: dict[str, str] = {}

    async def fetch(url: str) -> str:
        seen["url"] = url
        return _tags_page("llama3.1", LLAMA_TAGS)

    lookup = VariantLookup(library_url="https://lib.example/library/", fetch=fetch)
    resp = await lookup.variants("llama3.1:8b")

    # The tags page (not the registry's non-existent tags/list) is fetched, under the base.
    assert seen["url"] == "https://lib.example/library/llama3.1/tags"
    assert resp.model == "llama3.1:8b"
    tags = {v.tag for v in resp.variants}
    assert "llama3.1:8b-instruct-q8_0" in tags
    assert "llama3.1:8b" in tags
    assert all(":70b" not in t for t in tags)  # filtered to the requested size
    assert all("other-model" not in t for t in tags)  # an unrelated model is ignored


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
