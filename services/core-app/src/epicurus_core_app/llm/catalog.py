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

On-disk sizes (#571): the index page publishes none, so the refresh alone leaves every
live entry's ``size_gb`` empty. A background **size fill** backfills them from each
family's *tags page* — the same fetch the quant-variant lookup (#330) already does,
shared through its per-family cache — one family per ``size_fill_seconds``, most-popular
first, so the refresh itself stays exactly one upstream request and the fill is polite.
A tags-page failure just leaves that family size-less; it never blocks the catalog.
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
from epicurus_core_app.llm.variants import TagInfo

log = get_logger("epicurus_core_app.llm.catalog")

# The capability/tag vocabulary the web browser knows (data/catalog.ts). The parser
# only ever emits tags from this set, in this order, so the response stays stable.
KNOWN_TAGS: tuple[str, ...] = (
    "general",
    "code",
    "multilingual",
    "vision",
    "tools",
    "thinking",
    "embedding",
    "small",
    "cloud",
)

# A model is "small" when its largest-listed parameter count is under this many billions.
_SMALL_PARAMS_B = 2.0

_USER_AGENT = "epicurus-core/model-catalog (+https://github.com/baakhoff/epicurus)"

# How the model library marks each block. The page used to carry ``x-test-*`` hooks and the
# parser keyed on them; the 2026 redesign dropped every one, which parsed the catalog to []
# and wedged the box on the seed (#710). The selectors below therefore key on the page's
# *structure and user-visible copy* — the per-model link, the badge idiom, the word "Pulls" —
# never on the Tailwind colours that distinguish the chip kinds. Copy and structure survive a
# restyle; colours are the first thing a redesign changes. Verified against all 233 blocks of
# the live index, 2026-07-25 (``tests/fixtures/ollama-library.html`` is a trimmed capture).
#
# One model = one library anchor: ``<a href="/library/NAME" class="…">…</a>`` (not nested).
_MODEL_BLOCK = re.compile(r'<a\b[^>]*\bhref="/library/([^"?#]+)"[^>]*>(.*?)</a>', re.S)
_PARA = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S)
_TAG = re.compile(r"<[^>]+>")

# Every chip — capability, parameter size, and the cloud pill alike — is a rounded badge span;
# its *text* says which kind it is. Classifying by text rather than by the per-kind colour
# (``bg-indigo-50`` / ``bg-[#ddf4ff]`` / ``bg-cyan-50``) means a palette change degrades to
# "no chips parsed" instead of silently reading sizes as capabilities.
_PILL = re.compile(r'<span\b[^>]*\bclass="[^"]*\brounded-md\b[^"]*"[^>]*>([^<]*)</span>')
# A parameter-size chip: "8b", "1.5b", "270m", a mixture-of-experts "8x7b" / "128x17b", or a
# Gemma-3n "effective" size "e2b". Every other chip on the row is a capability (incl. "cloud").
_SIZE_LABEL = re.compile(r"^(?:e|\d+x)?\d[\d.]*[bm]$", re.I)

# The stats line renders each count in its own element immediately before its label, e.g.
# ``<span>117.7M</span><span>&nbsp;Pulls</span>`` — so the pull count is the last count
# element before the word "Pulls".
_PULLS_LABEL = re.compile(r"(?:\s|&nbsp;)*Pulls\b", re.I)
_COUNT = re.compile(r">\s*([\d.,]+\s*[kmb]?)\s*<", re.I)
# Those same labels are whole elements, never prose — which is how the stats paragraph is told
# apart from a blurb. The previous word-boundary guard (``\bupdated\b`` over the stripped text)
# blanked any description that merely *said* "updated"/"tags"/"pulls": 7 of 233 families,
# mistral's "…updated to version 0.3." among them.
_STATS_LABEL = re.compile(r">(?:\s|&nbsp;)*(?:Pulls|Tags|Updated)(?:\s|&nbsp;)*<", re.I)


class CatalogEntry(BaseModel):
    """One browsable, pullable model entry — the web's ``CatalogEntry`` shape."""

    id: str  # pullable ref, e.g. "llama3.1:8b" or (size-less) "nomic-embed-text"
    family: str  # display/group name, e.g. "llama3.1"
    params: str = ""  # size label, e.g. "8b"; "" for a size-less family
    # On-disk size in GB. The library *index* does not publish it, so a fresh parse leaves it
    # None; the seed and the tags-page size fill (#571) supply it. Always None for cloud rows.
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
    """Parse a pull-count label (``116.3M``, ``1M``, ``8,171``) to an int for ordering.

    Counts below 10K are rendered in full with thousands separators (``8,171``), so the
    separator has to be tolerated or the least-pulled models all rank 0 and sort arbitrarily.
    """
    if not label:
        return 0
    match = re.fullmatch(r"\s*([\d.,]+)\s*([kmb]?)\s*", label.strip(), re.I)
    if not match:
        return 0
    scale = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[match.group(2).lower()]
    try:
        return int(float(match.group(1).replace(",", "")) * scale)
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
    if "thinking" in caps:
        tags.add("thinking")
    # "cloud" marks an entry with no local weights — only the *size-less* row of a
    # cloud-pilled family qualifies. Hybrid families (gemma3, gpt-oss, …) carry the pill
    # too, but their size-expanded rows are ordinary downloadable builds and stay untagged.
    if "cloud" in caps and not params:
        tags.add("cloud")
    haystack = f"{name} {description}".lower()
    if re.search(r"\bcod(?:e|er|ing)\b", haystack):
        tags.add("code")
    # "multiling" is a deliberate prefix (multilingual, multilinguality) — leading boundary only.
    if re.search(r"\bmultiling|\blanguages\b", haystack):
        tags.add("multilingual")
    billions = _params_to_billions(params)
    if billions is not None and billions < _SMALL_PARAMS_B:
        tags.add("small")
    # Every non-embedding model is a general chat model (vision/code ones included).
    if "embedding" not in tags:
        tags.add("general")
    return [tag for tag in KNOWN_TAGS if tag in tags]


def _find_pulls(body: str) -> str | None:
    """The pull-count label from a block's stats line, or None when it publishes none."""
    label = _PULLS_LABEL.search(body)
    if label is None:
        return None
    counts = [m.group(1).strip() for m in _COUNT.finditer(body, 0, label.start())]
    return counts[-1] if counts else None


def _parse_block(name: str, body: str, order: int) -> _RawModel | None:
    """Parse one library model anchor into a :class:`_RawModel` (None when it isn't one)."""
    name = name.strip()
    # A ``family:tag`` ref or a nested path is a *tags*-page link, never a library entry —
    # so a tags page fed here parses to nothing rather than to a catalog of bogus families.
    if not name or ":" in name or "/" in name:
        return None

    # The blurb is the first paragraph that isn't the pulls/tags/updated stats line.
    description = ""
    for para in _PARA.finditer(body):
        if not _STATS_LABEL.search(para.group(1)):
            description = _strip_html(para.group(1))
            break

    sizes: list[str] = []
    caps: set[str] = set()
    for raw in _PILL.findall(body):
        chip = raw.strip()
        if not chip:
            continue
        if _SIZE_LABEL.match(chip):
            if chip.lower() not in sizes:
                sizes.append(chip.lower())
        else:
            caps.add(chip.lower())

    pulls = _find_pulls(body)
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
    for order, match in enumerate(_MODEL_BLOCK.finditer(document)):
        parsed = _parse_block(match.group(1), match.group(2), order)
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


# The size fill's source of tags-page rows — ``VariantLookup.family_tags`` in the app.
# Injected as a callable so the catalog stays decoupled from (and testable without) the lookup.
TagSource = Callable[[str], Awaitable[list[TagInfo]]]


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
        tag_source: Per-family tags-page rows for the GB size fill (#571) — the variant
            lookup's cached ``family_tags`` in the app. None disables all size enrichment.
        size_fill_seconds: Pause between background size-fill lookups (rate limit); 0
            disables the background fill (on-demand enrichment still works).
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
        tag_source: TagSource | None = None,
        size_fill_seconds: float = 30.0,
    ) -> None:
        self._source = source_url
        self._refresh_seconds = max(60.0, refresh_seconds)
        self._max_models = max(0, max_models)
        self._enabled = enabled
        self._fetch = fetch or _httpx_fetcher(timeout)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._tag_source = tag_source
        self._size_fill_seconds = max(0.0, size_fill_seconds)
        self._lock = asyncio.Lock()
        # Start on the seed, flagged stale until the first successful parse lands.
        self._entries: list[CatalogEntry] = list(seed if seed is not None else SEED_CATALOG)
        self._updated_at: datetime | None = None
        self._stale = True
        # Size-fill bookkeeping: each successful refresh bumps the generation, which restarts
        # the fill pass; families already attempted this pass are skipped so a family whose
        # tags page yields no sizes (cloud-only) can't wedge the walk.
        self._generation = 0
        self._fill_generation = -1
        self._fill_attempted: set[str] = set()
        # Failure-streak bookkeeping, so a *persistently* broken upstream is reported once
        # rather than every ``refresh_seconds`` forever (#710): the box logged the same warn
        # every few minutes for weeks while ollama.com's redesign broke the parse.
        self._failures = 0
        self._last_error = ""

    async def refresh(self) -> bool:
        """Fetch + parse the source once, swapping in the result on success.

        Never raises (except on cancellation): a fetch/parse failure or an empty parse
        leaves the previous snapshot in place and flags it stale. Returns whether the
        snapshot was updated.

        Logging is bounded by failure streak (#710): the first failure warns, and so does a
        *change* of error (a new symptom is news), but a streak of the same failure drops to
        debug — a broken upstream must not write a warn every few minutes indefinitely.
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
            error = str(exc)
            async with self._lock:
                self._stale = True
                first = self._failures == 0 or error != self._last_error
                self._failures += 1
                failures = self._failures
                self._last_error = error
            if first:
                log.warning(
                    "model catalog refresh failed; serving last-good/seed",
                    source=self._source,
                    error=error,
                )
            else:
                log.debug(
                    "model catalog refresh still failing",
                    source=self._source,
                    error=error,
                    failures=failures,
                )
            return False
        async with self._lock:
            # Carry known sizes across the swap: a fresh index parse has size_gb=None
            # everywhere, and dropping the enriched values would blank every GB label
            # until the fill pass reaches each family again (#571).
            known = {e.id: e.size_gb for e in self._entries if e.size_gb is not None}
            entries = [
                e.model_copy(update={"size_gb": known[e.id]})
                if e.size_gb is None and e.id in known and "cloud" not in e.tags
                else e
                for e in entries
            ]
            self._entries = entries
            self._updated_at = self._clock()
            self._stale = False
            self._generation += 1
            # Close the streak, and report how long it ran — a recovery after a debug-quiet
            # stretch would otherwise be the one thing the operator never sees.
            recovered_after = self._failures
            self._failures = 0
            self._last_error = ""
        log.info(
            "model catalog refreshed",
            source=self._source,
            entries=len(entries),
            **({"recovered_after": recovered_after} if recovered_after else {}),
        )
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

    async def enrich_family(self, family: str) -> bool:
        """Backfill ``family``'s entries' ``size_gb`` from its tags-page rows (#571).

        Pulls the rows through the injected ``tag_source`` (the variant lookup's per-family
        cache, so a lookup the UI just did costs no second request). Never raises — a fetch
        or parse failure leaves the entries as they are. Returns whether anything changed.
        """
        if self._tag_source is None or not family:
            return False
        try:
            tags = await self._tag_source(family)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("catalog size lookup failed", family=family, error=str(exc))
            return False
        return await self._apply_sizes(family, tags)

    async def _apply_sizes(self, family: str, tags: list[TagInfo]) -> bool:
        """Fold tags-page sizes into ``family``'s entries; True when an entry changed.

        A sized row (``params`` set) takes its bare tag's size — the default build. A
        size-less downloadable row takes ``latest`` (its pullable alias), falling back to
        the first sized tag. Cloud rows are skipped — no local weights, no size, by design.
        """
        sizes = {t.tag.strip().lower(): t.size_gb for t in tags if t.size_gb is not None}
        if not sizes:
            return False
        fallback = sizes["latest"] if "latest" in sizes else next(iter(sizes.values()))
        changed = False
        async with self._lock:
            updated = list(self._entries)
            for i, entry in enumerate(updated):
                if entry.family != family or "cloud" in entry.tags:
                    continue
                size = sizes.get(entry.params) if entry.params else fallback
                if size is not None and size != entry.size_gb:
                    updated[i] = entry.model_copy(update={"size_gb": size})
                    changed = True
            if changed:
                self._entries = updated
        return changed

    async def fill_step(self) -> None:
        """One size-fill step: enrich the most-popular family still missing a size.

        Entries are already popularity-ordered, so "first missing" is "most popular
        missing". Every attempted family is remembered for the current catalog generation —
        success or not — so a family with nothing to offer (cloud-only) is visited once per
        pass instead of wedging the walk.
        """
        async with self._lock:
            if self._generation != self._fill_generation:
                self._fill_generation = self._generation
                self._fill_attempted.clear()
            family = next(
                (
                    e.family
                    for e in self._entries
                    if e.size_gb is None
                    and "cloud" not in e.tags
                    and e.family not in self._fill_attempted
                ),
                None,
            )
        if family is None:
            return  # pass complete — idle until a refresh swaps in a new list
        self._fill_attempted.add(family)
        await self.enrich_family(family)
        async with self._lock:
            pending = {
                e.family
                for e in self._entries
                if e.size_gb is None
                and "cloud" not in e.tags
                and e.family not in self._fill_attempted
            }
        if pending:
            log.debug("catalog size fill", family=family, pending=len(pending))
        else:
            log.info("catalog size fill pass complete", last_family=family)

    async def run_size_fill(self) -> None:
        """Backfill GB sizes in the background, one family per ``size_fill_seconds`` (#571).

        Launched as its own task next to :meth:`run_periodic`. Deliberately not an eager
        crawl: one rate-limited tags-page lookup at a time (shared with the variant lookup's
        cache), restarting the walk only when a refresh lands a new list. Disabled catalogs
        (air-gapped) and missing sources never fetch.
        """
        if not self._enabled or self._tag_source is None or self._size_fill_seconds <= 0:
            log.info("catalog size fill disabled", source=self._source)
            return
        while True:
            await asyncio.sleep(self._size_fill_seconds)
            await self.fill_step()

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
