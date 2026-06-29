"""Quant-variant lookup — on-demand enumeration of a model's quantizations (#330).

The browse catalog (``catalog.py``, #269) parses the model *library* index, which lists each
model's parameter **sizes** but not its **quantizations**. To pull a different quant today the
operator has to already know the exact tag (e.g. ``llama3.1:8b-instruct-q8_0``) and type it.

This module fetches a model's **tags page** on demand — ``<library>/<family>/tags`` (the same
host the catalog parses) — and pulls the tags belonging to a given size into a small list of
quant variants the Models page renders as a pick-list. (The OCI registry's ``tags/list`` JSON
endpoint is *not* an option — ``registry.ollama.ai`` returns 404 for it; only the public tags
page enumerates a model's quants.) It is deliberately best-effort: any failure (offline, a model
not in the public library, a malformed page) yields an empty list, and the UI falls back to the
manual tag box. Like the catalog it is global, not tenant-scoped — it mirrors a public library.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable

import httpx
from pydantic import BaseModel

from epicurus_core import get_logger

log = get_logger("epicurus_core_app.llm.variants")

_USER_AGENT = "epicurus-core/model-variants (+https://github.com/baakhoff/epicurus)"

# A quantization token inside a tag, delimited by start/end or a ``-``/``.`` separator:
# matches ``q4_K_M``, ``q8_0``, ``iq4_xs``, ``fp16``, ``bf16``, ``f16``, ``f32``.
_QUANT = re.compile(r"(?:^|[-.])((?:iq|q)\d+(?:_[a-z0-9]+)*|fp16|bf16|f16|f32)(?=$|[-.])", re.I)

# Each pullable tag on a library tags page is a ``/library/<family>:<tag>`` link (the same href
# hook the catalog parses, which has stayed stable across the page's redesigns).
_TAG_HREF = re.compile(r'href="/library/([^"?#]+)"')


class ModelVariant(BaseModel):
    """One pullable quantization of a model size."""

    tag: str  # the full pullable ref, e.g. "llama3.1:8b-instruct-q8_0"
    quant: str  # the parsed quant label, e.g. "q8_0" / "fp16"; "" for the default build


class ModelVariantsResponse(BaseModel):
    """The quant variants available for a model, served at ``/llm/catalog/variants``."""

    model: str  # the model the variants were looked up for, e.g. "llama3.1:8b"
    variants: list[ModelVariant] = []


def parse_variant_tags(family: str, size: str, tags: list[str]) -> list[ModelVariant]:
    """Pick the quant variants for ``size`` out of a family's full tag list.

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


def tags_from_page(family: str, document: str) -> list[str]:
    """The bare tags for ``family`` parsed from its library *tags* page (deduped, page order).

    The page lists each pullable tag as a ``/library/<family>:<tag>`` link. The family's own
    index link (no ``:<tag>``) and links to other models are ignored, so the result is exactly
    the tag strings :func:`parse_variant_tags` expects (e.g. ``8b``, ``8b-instruct-q8_0``).
    """
    out: list[str] = []
    seen: set[str] = set()
    for ref in _TAG_HREF.findall(document):
        fam, sep, tag = ref.partition(":")
        if not sep or fam != family or not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


# A text fetcher takes a URL and returns the response body; injected in tests to avoid the network.
TextFetcher = Callable[[str], Awaitable[str]]


def _httpx_text_fetcher(timeout: float) -> TextFetcher:
    """The default fetcher — an httpx GET that returns the response text (the tags HTML page)."""

    async def fetch(url: str) -> str:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    return fetch


class VariantLookup:
    """Enumerates a model's quant variants from its public library tags page, on demand.

    Args:
        library_url: The library base (Ollama's public library by default), the same base the
            catalog parses; a model's tags page is ``<library_url>/<family>/tags``.
        fetch: The text fetcher; injected in tests. Defaults to an httpx GET.
        timeout: Per-request timeout for the default fetcher.
    """

    def __init__(
        self,
        *,
        library_url: str,
        fetch: TextFetcher | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._library = library_url.rstrip("/")
        self._fetch = fetch or _httpx_text_fetcher(timeout)

    async def variants(self, model: str) -> ModelVariantsResponse:
        """The quant variants for ``model`` (e.g. ``llama3.1:8b``). Never raises: a lookup
        failure (offline, a model not in the public library, a malformed page) returns an empty
        list and the UI falls back to the manual tag box."""
        family, _, size = model.partition(":")
        family = family.strip()
        if not family:
            return ModelVariantsResponse(model=model, variants=[])
        url = f"{self._library}/{family}/tags"
        try:
            document = await self._fetch(url)
            tags = tags_from_page(family, document)
            variants = parse_variant_tags(family, size, tags)
        except httpx.HTTPStatusError as exc:
            # 404 = a model not in the public library (a local/custom model); expected, not
            # alarming — log it quietly so the picker's normal misses don't spam warnings.
            status = exc.response.status_code
            if status == 404:
                log.debug("variant lookup: model not in library", model=model)
            else:
                log.warning("variant lookup failed; serving none", model=model, status=status)
            return ModelVariantsResponse(model=model, variants=[])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("variant lookup failed; serving none", model=model, error=str(exc))
            return ModelVariantsResponse(model=model, variants=[])
        return ModelVariantsResponse(model=model, variants=variants)
