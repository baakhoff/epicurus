"""Live model catalog — the core parses an upstream model library on a schedule (#269).

The model browser used to read a hand-maintained static list baked into the web build
(``services/web/src/data/catalog.ts``); it went stale and forced a code change + web
release for every new model. Per constraint #8 (all model/LLM concerns live in the
core), the core now owns the catalog: it fetches a configurable source (the public
Ollama library by default), parses it into browse entries, caches the result, and
refreshes it **regularly** on a background loop. The web shell fetches
``GET /platform/v1/llm/catalog`` and renders it through the same ``filterCatalog``.

Resilience: a fetch or parse failure keeps the last-good snapshot; before anything has
been fetched (cold start, or an air-gapped build with ``LLM_CATALOG_ENABLED=false``) the
catalog serves a small built-in **seed** so the browser is never empty. The catalog is
global, not tenant-scoped — it mirrors a public registry, holds no tenant data, and is
identical for every tenant (like the provider registry).
"""

from __future__ import annotations

import asyncio
import html
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel

from epicurus_core import get_logger

log = get_logger("epicurus_core_app.llm.catalog")

# The capability/tag vocabulary the web browser knows (data/catalog.ts). The parser
# only ever emits tags from this set, in this order, so the response stays stable.
KNOWN_TAGS: tuple[str, ...] = (
    "general",
    "code",
    "multilingual",
    "vision",
    "tools",
    "embedding",
    "small",
)

# A model is "small" when its largest-listed parameter count is under this many billions.
_SMALL_PARAMS_B = 2.0

_USER_AGENT = "epicurus-core/model-catalog (+https://github.com/baakhoff/epicurus)"

# How the model library marks each block. These ``x-test-*`` hooks are the page's own
# test anchors and have been stable across redesigns — far steadier than CSS classes.
_MODEL_BLOCK = re.compile(r"<li\b[^>]*\bx-test-model\b.*?</li>", re.S)
_HREF = re.compile(r'href="/library/([^"?#]+)"')
_TITLE = re.compile(r'x-test-model-title[^>]*\btitle="([^"]*)"')
_TITLE_MARK = re.compile(r"x-test-model-title")
_PARA = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S)
_SIZE = re.compile(r"x-test-size[^>]*>([^<]+)<")
_CAP = re.compile(r"x-test-capability[^>]*>([^<]+)<")
_PULLS = re.compile(r"x-test-pull-count[^>]*>([^<]+)<")
_TAG = re.compile(r"<[^>]+>")


class CatalogEntry(BaseModel):
    """One browsable, pullable model entry — the web's ``CatalogEntry`` shape."""

    id: str  # pullable ref, e.g. "llama3.1:8b" or (size-less) "nomic-embed-text"
    family: str  # display/group name, e.g. "llama3.1"
    params: str = ""  # size label, e.g. "8b"; "" for a size-less family
    # The library does not publish on-disk size; None unless the seed supplies it.
    size_gb: float | None = None
    description: str = ""
    tags: list[str] = []
    pulls: str | None = None  # popularity label as shown upstream, e.g. "116.3M"


class CatalogResponse(BaseModel):
    """The catalog snapshot served at ``GET /platform/v1/llm/catalog``."""

    entries: list[CatalogEntry]
    source: str  # where the list was parsed from (or the configured source when seeded)
    updated_at: datetime | None = None  # last successful parse; None while seeded
    stale: bool = False  # True when serving the seed / last-good after a failure


# A small, always-available seed so the browser is never empty offline or pre-fetch.
# Kept intentionally short (the live parse supplies the full list); these are the
# evergreen defaults an operator is most likely to want on a fresh, air-gapped box.
SEED_CATALOG: list[CatalogEntry] = [
    CatalogEntry(
        id="llama3.2:3b",
        family="llama3.2",
        params="3b",
        size_gb=2.0,
        description="Meta's compact all-rounder — fits on any modern laptop.",
        tags=["general", "small"],
    ),
    CatalogEntry(
        id="llama3.1:8b",
        family="llama3.1",
        params="8b",
        size_gb=4.9,
        description="128K-context Llama from Meta — strong general assistant.",
        tags=["general"],
    ),
    CatalogEntry(
        id="qwen2.5:7b",
        family="qwen2.5",
        params="7b",
        size_gb=4.7,
        description="Alibaba's multilingual model — excellent reasoning across 29 languages.",
        tags=["general", "multilingual"],
    ),
    CatalogEntry(
        id="qwen2.5-coder:7b",
        family="qwen2.5-coder",
        params="7b",
        size_gb=4.7,
        description="Best-in-class open code model at 7B — completions and debugging.",
        tags=["code"],
    ),
    CatalogEntry(
        id="gemma3:4b",
        family="gemma3",
        params="4b",
        size_gb=2.6,
        description="Balanced Google model with strong multilingual support.",
        tags=["general", "multilingual"],
    ),
    CatalogEntry(
        id="phi4-mini:3.8b",
        family="phi4-mini",
        params="3.8b",
        size_gb=2.5,
        description="Microsoft's efficiency-first model; strong instruction following.",
        tags=["general", "small"],
    ),
    CatalogEntry(
        id="llava:7b",
        family="llava",
        params="7b",
        size_gb=4.7,
        description="Open multimodal model — describe and analyse images.",
        tags=["vision"],
    ),
    CatalogEntry(
        id="nomic-embed-text",
        family="nomic-embed-text",
        params="",
        size_gb=0.27,
        description="Fast, high-quality text embeddings — the go-to for local RAG.",
        tags=["embedding", "small"],
    ),
]


@dataclass(slots=True)
class _RawModel:
    """A model parsed from one library block, before per-size expansion."""

    name: str
    description: str
    sizes: list[str]
    caps: set[str]
    pulls: str | None
    rank: int  # popularity, parsed from the pull count, for ordering/capping
    order: int = field(default=0)  # source order, a stable tiebreaker


def _strip_html(fragment: str) -> str:
    """Plain text from an HTML fragment: drop tags, unescape entities, collapse space."""
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", fragment))).strip()


def _params_to_billions(label: str) -> float | None:
    """Parse a size label (``8b``, ``1.5b``, ``270m``) to a parameter count in billions."""
    match = re.fullmatch(r"\s*([\d.]+)\s*([bm])\s*", label, re.I)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value / 1000 if match.group(2).lower() == "m" else value


def _pulls_to_rank(label: str | None) -> int:
    """Parse a pull-count label (``116.3M``, ``1M``, ``500``) to an int for ordering."""
    if not label:
        return 0
    match = re.fullmatch(r"\s*([\d.]+)\s*([kmb]?)\s*", label.strip(), re.I)
    if not match:
        return 0
    scale = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[match.group(2).lower()]
    try:
        return int(float(match.group(1)) * scale)
    except ValueError:
        return 0


def _derive_tags(name: str, description: str, caps: set[str], params: str) -> list[str]:
    """Map a model's capabilities + heuristics onto the web's known tag vocabulary."""
    tags: set[str] = set()
    if "embedding" in caps:
        tags.add("embedding")
    if "vision" in caps:
        tags.add("vision")
    if "tools" in caps:
        tags.add("tools")
    haystack = f"{name} {description}".lower()
    if re.search(r"cod(?:e|er|ing)", haystack):
        tags.add("code")
    if re.search(r"multiling|languages", haystack):
        tags.add("multilingual")
    billions = _params_to_billions(params)
    if billions is not None and billions < _SMALL_PARAMS_B:
        tags.add("small")
    # Every non-embedding model is a general chat model (vision/code ones included).
    if "embedding" not in tags:
        tags.add("general")
    return [tag for tag in KNOWN_TAGS if tag in tags]


def _parse_block(block: str, order: int) -> _RawModel | None:
    """Parse one ``<li x-test-model>`` block into a :class:`_RawModel` (None if nameless)."""
    href = _HREF.search(block)
    title = _TITLE.search(block)
    name = (href.group(1) if href else title.group(1) if title else "").strip()
    if not name:
        return None

    # The description is the first <p> after the title marker (the title div's blurb).
    # A later <p> holds the pulls/tags/updated stats; guard against grabbing that one.
    description = ""
    mark = _TITLE_MARK.search(block)
    para = _PARA.search(block, mark.end() if mark else 0)
    if para:
        text = _strip_html(para.group(1))
        if not re.search(r"\b(pulls|tags|updated)\b", text, re.I):
            description = text

    sizes: list[str] = []
    for raw in _SIZE.findall(block):
        size = raw.strip().lower()
        if size and size not in sizes:
            sizes.append(size)
    caps = {c.strip().lower() for c in _CAP.findall(block) if c.strip()}
    pulls_match = _PULLS.search(block)
    pulls = pulls_match.group(1).strip() if pulls_match else None
    return _RawModel(
        name=name,
        description=description,
        sizes=sizes,
        caps=caps,
        pulls=pulls,
        rank=_pulls_to_rank(pulls),
        order=order,
    )


def parse_library(document: str, *, max_models: int = 0) -> list[CatalogEntry]:
    """Parse a model-library HTML page into catalog entries, most-popular first.

    Each upstream model lists zero or more parameter sizes; we emit one pullable entry
    per size (``"<name>:<size>"``), or a single size-less entry (``"<name>"``) for models
    that publish none (e.g. embedding models). ``max_models`` caps the number of model
    *families* kept (after sorting by popularity); ``0`` means unlimited. Returns ``[]``
    for an empty or unrecognised document — the caller treats that as a failed refresh.
    """
    models: list[_RawModel] = []
    for order, block in enumerate(_MODEL_BLOCK.findall(document)):
        parsed = _parse_block(block, order)
        if parsed is not None:
            models.append(parsed)

    # Most-pulled first; source order breaks ties so the result is deterministic.
    models.sort(key=lambda m: (-m.rank, m.order))
    if max_models > 0 and len(models) > max_models:
        log.info(
            "model catalog capped to most-popular families",
            kept=max_models,
            dropped=len(models) - max_models,
        )
        models = models[:max_models]

    entries: list[CatalogEntry] = []
    seen: set[str] = set()
    for model in models:
        # Expand to one entry per size; a size-less model becomes a single entry.
        for size in model.sizes or [""]:
            entry_id = f"{model.name}:{size}" if size else model.name
            if entry_id in seen:
                continue
            seen.add(entry_id)
            entries.append(
                CatalogEntry(
                    id=entry_id,
                    family=model.name,
                    params=size,
                    description=model.description,
                    tags=_derive_tags(model.name, model.description, model.caps, size),
                    pulls=model.pulls,
                )
            )
    return entries


# A fetcher takes a URL and returns the page body; injected in tests to avoid the network.
Fetcher = Callable[[str], Awaitable[str]]


def _httpx_fetcher(timeout: float) -> Fetcher:
    """The default fetcher — an httpx GET with a redirect-following client."""

    async def fetch(url: str) -> str:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    return fetch


class ModelCatalog:
    """Owns the model list: refreshes it from upstream on a loop, serves a cached snapshot.

    Args:
        source_url: Where to parse the model list from (the Ollama library by default).
        refresh_seconds: How often the background loop re-parses the source.
        max_models: Cap on model families kept (0 = unlimited); the most-popular survive.
        enabled: When False, no outbound fetch happens — the seed is served as-is.
        seed: The built-in fallback served before the first successful parse.
        fetch: The page fetcher; injected in tests. Defaults to an httpx GET.
        timeout: Per-request timeout for the default fetcher.
        clock: Returns "now"; injected in tests for a deterministic ``updated_at``.
    """

    def __init__(
        self,
        *,
        source_url: str,
        refresh_seconds: float,
        max_models: int = 0,
        enabled: bool = True,
        seed: list[CatalogEntry] | None = None,
        fetch: Fetcher | None = None,
        timeout: float = 15.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = source_url
        self._refresh_seconds = max(60.0, refresh_seconds)
        self._max_models = max(0, max_models)
        self._enabled = enabled
        self._fetch = fetch or _httpx_fetcher(timeout)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        # Start on the seed, flagged stale until the first successful parse lands.
        self._entries: list[CatalogEntry] = list(seed if seed is not None else SEED_CATALOG)
        self._updated_at: datetime | None = None
        self._stale = True

    async def refresh(self) -> bool:
        """Fetch + parse the source once, swapping in the result on success.

        Never raises (except on cancellation): a fetch/parse failure or an empty parse
        leaves the previous snapshot in place and flags it stale. Returns whether the
        snapshot was updated.
        """
        if not self._enabled:
            return False
        try:
            document = await self._fetch(self._source)
            entries = parse_library(document, max_models=self._max_models)
            if not entries:
                raise ValueError("parsed catalog was empty")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self._lock:
                self._stale = True
            log.warning(
                "model catalog refresh failed; serving last-good/seed",
                source=self._source,
                error=str(exc),
            )
            return False
        async with self._lock:
            self._entries = entries
            self._updated_at = self._clock()
            self._stale = False
        log.info("model catalog refreshed", source=self._source, entries=len(entries))
        return True

    async def snapshot(self) -> CatalogResponse:
        """The current cached catalog, safe to serve directly to the web shell."""
        async with self._lock:
            return CatalogResponse(
                entries=list(self._entries),
                source=self._source,
                updated_at=self._updated_at,
                stale=self._stale,
            )

    async def run_periodic(self) -> None:
        """Refresh now, then every ``refresh_seconds`` until cancelled (app shutdown).

        Launched as a background task so app startup never blocks on the network; a
        disabled catalog returns immediately and just serves the seed.
        """
        if not self._enabled:
            log.info("model catalog disabled; serving the built-in seed", source=self._source)
            return
        while True:
            await self.refresh()
            await asyncio.sleep(self._refresh_seconds)
