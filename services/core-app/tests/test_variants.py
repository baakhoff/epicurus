"""Tests for the on-demand quant-variant lookup (#330).

The tag parser is pure and exercised directly; ``VariantLookup`` is driven with an injected
JSON fetcher so no test touches the network.
"""

from __future__ import annotations

from typing import Any

from epicurus_core_app.llm.variants import VariantLookup, parse_variant_tags

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


async def test_lookup_returns_variants_via_injected_fetcher() -> None:
    seen: dict[str, str] = {}

    async def fetch(url: str) -> dict[str, Any]:
        seen["url"] = url
        return {"name": "library/llama3.1", "tags": LLAMA_TAGS}

    lookup = VariantLookup(registry_url="https://registry.example/", fetch=fetch)
    resp = await lookup.variants("llama3.1:8b")

    assert seen["url"] == "https://registry.example/v2/library/llama3.1/tags/list"
    assert resp.model == "llama3.1:8b"
    tags = {v.tag for v in resp.variants}
    assert "llama3.1:8b-instruct-q8_0" in tags
    assert all(":70b" not in t for t in tags)


async def test_lookup_is_best_effort_on_failure() -> None:
    async def fetch(url: str) -> dict[str, Any]:
        raise RuntimeError("offline")

    lookup = VariantLookup(registry_url="https://registry.example", fetch=fetch)
    resp = await lookup.variants("llama3.1:8b")
    assert resp.variants == []


async def test_lookup_skips_fetch_for_a_blank_model() -> None:
    called = False

    async def fetch(url: str) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"tags": []}

    lookup = VariantLookup(registry_url="https://registry.example", fetch=fetch)
    resp = await lookup.variants("")
    assert resp.variants == []
    assert called is False
