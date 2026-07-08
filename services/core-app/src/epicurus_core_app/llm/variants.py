"""Quant-variant lookup — on-demand enumeration of a model's quantizations (#330, #571).

The browse catalog (``catalog.py``, #269) parses the model *library* index, which lists each
model's parameter **sizes** but not its **quantizations** — and no on-disk sizes at all.
To pull a different quant today the operator has to already know the exact tag
(e.g. ``llama3.1:8b-instruct-q8_0``) and type it.

This module fetches a model's **tags page** on demand — ``<library>/<family>/tags`` (the same
host the catalog parses) — and pulls the tags belonging to a given size into a small list of
quant variants the Models page renders as a pick-list. (The OCI registry's ``tags/list`` JSON
endpoint is *not* an option — ``registry.ollama.ai`` returns 404 for it; only the public tags
page enumerates a model's quants.) The tags page also publishes each downloadable tag's
**on-disk size** ("4.9GB", "274MB"; cloud tags have none) — the parse captures it per tag
(#571), so the pick-list shows real sizes and the catalog can backfill its GB labels from the
same fetch. Parsed tag rows are cached per family with a TTL so repeated lookups and the
catalog's background size fill share one upstream request per family. It is deliberately
best-effort: any failure (offline, a model not in the public library, a malformed page) yields
an empty list, and the UI falls back to the manual tag box. Like the catalog it is global, not
tenant-scoped — it mirrors a public library.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

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

# A size label as the tags page renders it next to each downloadable tag: "4.9GB", "731MB",
# "1.2TB" (verified live 2026-07-09). "128K context window" and blob hashes never match.
_SIZE_TEXT = re.compile(r"\b(\d+(?:\.\d+)?)\s*(TB|GB|MB)\b")

# How far past a tag link to look for its row's size. A row (either the mobile or the desktop
# layout) is well under 2 KB of markup; the cap keeps a footer or the page tail from ever
# donating a stray size token to the last row.
_ROW_WINDOW = 4000


@dataclass(slots=True, frozen=True)
class TagInfo:
    """One tag row parsed from a library tags page: the bare tag and its shown size."""

    tag: str
    size_gb: float | None = None


class ModelVariant(BaseModel):
    """One pullable quantization of a model size."""

    tag: str  # the full pullable ref, e.g. "llama3.1:8b-instruct-q8_0"
    quant: str  # the parsed quant label, e.g. "q8_0" / "fp16"; "" for the default build
    # On-disk size parsed from the tags page (#571); None when upstream shows none (cloud tags).
    size_gb: float | None = None


class ModelVariantsResponse(BaseModel):
    """The quant variants available for a model, served at ``/llm/catalog/variants``."""

    model: str  # the model the variants were looked up for, e.g. "llama3.1:8b"
    variants: list[ModelVariant] = []


def size_text_to_gb(text: str) -> float | None:
    """The first size token in ``text`` ("4.9GB", "731MB", "1.2TB") as GB, or None.

    Decimal units (1 GB = 1000 MB), matching both the library's own labels and the web's
    ``formatGb`` round-trip (0.274 → "274 MB").
    """
    match = _SIZE_TEXT.search(text)
    if not match:
        return None
    value = float(match.group(1))
    scale = {"MB": 0.001, "GB": 1.0, "TB": 1000.0}[match.group(2).upper()]
    return value * scale


def parse_variant_tags(family: str, size: str, tags: list[TagInfo]) -> list[ModelVariant]:
    """Pick the quant variants for ``size`` out of a family's parsed tag rows.

    Keeps tags that belong to the requested size (the bare ``<size>`` default build and any
    ``<size>-…`` variant); for a size-less family (embedding models) every tag qualifies.
    ``latest`` is dropped (it's an alias, not a distinct quant). Each kept tag becomes a
    ``<family>:<tag>`` ref with its quant parsed from the tag (``""`` when the tag carries no
    quant token — the default build) and the size its tags-page row showed.
    """
    size = size.strip().lower()
    out: list[ModelVariant] = []
    seen: set[str] = set()
    for info in tags:
        tag = info.tag.strip()
        low = tag.lower()
        if not tag or low == "latest":
            continue
        if size and not (low == size or low.startswith(f"{size}-")):
            continue
        if tag in seen:
            continue
        seen.add(tag)
        match = _QUANT.search(tag)
        out.append(
            ModelVariant(
                tag=f"{family}:{tag}",
                quant=match.group(1) if match else "",
                size_gb=info.size_gb,
            )
        )
    return out


def tags_from_page(family: str, document: str) -> list[TagInfo]:
    """The tag rows for ``family`` parsed from its library *tags* page (deduped, page order).

    The page lists each pullable tag as a ``/library/<family>:<tag>`` link. The family's own
    index link (no ``:<tag>``) and links to other models are ignored. Each tag's on-disk size
    is the first size token between its link and the next tag link (the page renders every row
    twice — a mobile and a desktop layout, both carrying the same size string — and the first
    occurrence wins); a row with no size token (a cloud tag) parses as ``size_gb=None``.
    """
    hits: list[tuple[str, int, int]] = []  # (tag, link end, link start)
    for match in _TAG_HREF.finditer(document):
        fam, sep, tag = match.group(1).partition(":")
        if sep and fam == family and tag:
            hits.append((tag, match.end(), match.start()))
    out: list[TagInfo] = []
    seen: set[str] = set()
    for i, (tag, start, _) in enumerate(hits):
        if tag in seen:
            continue
        seen.add(tag)
        end = hits[i + 1][2] if i + 1 < len(hits) else len(document)
        chunk = document[start:end][:_ROW_WINDOW]
        out.append(TagInfo(tag=tag, size_gb=size_text_to_gb(chunk)))
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
        cache_ttl_seconds: How long a family's parsed tag rows are served from cache before the
            page is re-fetched. Successes only — a failed fetch is never cached, so a transient
            outage recovers on the next call.
        now: Monotonic clock; injected in tests for deterministic TTL expiry.
    """

    def __init__(
        self,
        *,
        library_url: str,
        fetch: TextFetcher | None = None,
        timeout: float = 15.0,
        cache_ttl_seconds: float = 6 * 60 * 60,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._library = library_url.rstrip("/")
        self._fetch = fetch or _httpx_text_fetcher(timeout)
        self._ttl = max(0.0, cache_ttl_seconds)
        self._now = now or time.monotonic
        self._cache: dict[str, tuple[float, list[TagInfo]]] = {}

    async def family_tags(self, family: str) -> list[TagInfo]:
        """The parsed tag rows for ``family``, from cache or a fresh tags-page fetch.

        Raises on a fetch failure (callers choose how to degrade — :meth:`variants` serves an
        empty list, the catalog's size fill just skips the family). One upstream request per
        family per TTL, shared by every caller.
        """
        family = family.strip()
        if not family:
            return []
        cached = self._cache.get(family)
        if cached is not None and self._now() < cached[0]:
            return cached[1]
        document = await self._fetch(f"{self._library}/{family}/tags")
        tags = tags_from_page(family, document)
        self._cache[family] = (self._now() + self._ttl, tags)
        return tags

    async def variants(self, model: str) -> ModelVariantsResponse:
        """The quant variants for ``model`` (e.g. ``llama3.1:8b``). Never raises: a lookup
        failure (offline, a model not in the public library, a malformed page) returns an empty
        list and the UI falls back to the manual tag box."""
        family, _, size = model.partition(":")
        family = family.strip()
        if not family:
            return ModelVariantsResponse(model=model, variants=[])
        try:
            tags = await self.family_tags(family)
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
