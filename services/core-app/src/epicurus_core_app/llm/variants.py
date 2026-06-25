"""Quant-variant lookup — on-demand enumeration of a model's quantizations (#330).

The browse catalog (``catalog.py``, #269) parses the model *library* page, which lists each
model's parameter **sizes** but not its **quantizations**. To pull a different quant today the
operator has to already know the exact tag (e.g. ``llama3.1:8b-instruct-q8_0``) and type it.

This module queries the OCI **registry** on demand — ``/v2/library/<family>/tags/list`` — and
parses the tags belonging to a given size into a small list of quant variants the Models page
renders as a pick-list. It is deliberately best-effort: any failure (offline, 404 on a
non-library model, malformed body) yields an empty list, and the UI falls back to the manual
tag box. Like the catalog it is global, not tenant-scoped — it mirrors a public registry.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import BaseModel

from epicurus_core import get_logger

log = get_logger("epicurus_core_app.llm.variants")

_USER_AGENT = "epicurus-core/model-variants (+https://github.com/baakhoff/epicurus)"

# A quantization token inside a tag, delimited by start/end or a ``-``/``.`` separator:
# matches ``q4_K_M``, ``q8_0``, ``iq4_xs``, ``fp16``, ``bf16``, ``f16``, ``f32``.
_QUANT = re.compile(r"(?:^|[-.])((?:iq|q)\d+(?:_[a-z0-9]+)*|fp16|bf16|f16|f32)(?=$|[-.])", re.I)


class ModelVariant(BaseModel):
    """One pullable quantization of a model size."""

    tag: str  # the full pullable ref, e.g. "llama3.1:8b-instruct-q8_0"
    quant: str  # the parsed quant label, e.g. "q8_0" / "fp16"; "" for the default build


class ModelVariantsResponse(BaseModel):
    """The quant variants available for a model, served at ``/llm/catalog/variants``."""

    model: str  # the model the variants were looked up for, e.g. "llama3.1:8b"
    variants: list[ModelVariant] = []


def parse_variant_tags(family: str, size: str, tags: list[str]) -> list[ModelVariant]:
    """Pick the quant variants for ``size`` out of a family's full registry tag list.

    Keeps tags that belong to the requested size (the bare ``<size>`` default build and any
    ``<size>-…`` variant); for a size-less family (embedding models) every tag qualifies.
    ``latest`` is dropped (it's an alias, not a distinct quant). Each kept tag becomes a
    ``<family>:<tag>`` ref with its quant parsed from the tag (``""`` when the tag carries no
    quant token — the default build).
    """
    size = size.strip().lower()
    out: list[ModelVariant] = []
    seen: set[str] = set()
    for raw in tags:
        tag = raw.strip()
        low = tag.lower()
        if not tag or low == "latest":
            continue
        if size and not (low == size or low.startswith(f"{size}-")):
            continue
        if tag in seen:
            continue
        seen.add(tag)
        match = _QUANT.search(tag)
        out.append(ModelVariant(tag=f"{family}:{tag}", quant=match.group(1) if match else ""))
    return out


# A JSON fetcher takes a URL and returns the parsed body; injected in tests to avoid the network.
JsonFetcher = Callable[[str], Awaitable[dict[str, Any]]]


def _httpx_json_fetcher(timeout: float) -> JsonFetcher:
    """The default fetcher — an httpx GET that returns the parsed JSON body."""

    async def fetch(url: str) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            body: dict[str, Any] = response.json()
            return body

    return fetch


class VariantLookup:
    """Enumerates a model's quant variants from the OCI registry, on demand.

    Args:
        registry_url: The registry base (Ollama's public registry by default).
        fetch: The JSON fetcher; injected in tests. Defaults to an httpx GET.
        timeout: Per-request timeout for the default fetcher.
    """

    def __init__(
        self,
        *,
        registry_url: str,
        fetch: JsonFetcher | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._registry = registry_url.rstrip("/")
        self._fetch = fetch or _httpx_json_fetcher(timeout)

    async def variants(self, model: str) -> ModelVariantsResponse:
        """The quant variants for ``model`` (e.g. ``llama3.1:8b``). Never raises: a lookup
        failure (offline, non-library model, malformed body) returns an empty list."""
        family, _, size = model.partition(":")
        family = family.strip()
        if not family:
            return ModelVariantsResponse(model=model, variants=[])
        url = f"{self._registry}/v2/library/{family}/tags/list"
        try:
            body = await self._fetch(url)
            raw_tags = body.get("tags")
            if not isinstance(raw_tags, list):
                raise ValueError("registry response had no tags list")
            variants = parse_variant_tags(family, size, [str(t) for t in raw_tags])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("variant lookup failed; serving none", model=model, error=str(exc))
            return ModelVariantsResponse(model=model, variants=[])
        return ModelVariantsResponse(model=model, variants=variants)
