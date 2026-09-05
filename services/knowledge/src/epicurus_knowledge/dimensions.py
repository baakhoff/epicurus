"""Vector-dimension drift: keep a Qdrant collection matching the current embedder (#865).

A collection is created once, at whatever vector size the embedding model in force at the
time produced (``nomic-embed-text`` is 768-d; a hosted model is typically 1536-d or more).
Switch the embedding model and every later upsert carries vectors of a different size — which
Qdrant rejects with an opaque ``Vector dimension error``, and which no amount of retrying
fixes. The documented cure is the Models page's **Re-embed everything** (#332); this is the
safety net for an operator who switched the model without running it.

The heal is a **recreate**, not a re-embed in place: unlike the core's fact store (whose facts
are hand-distilled and have no source to recrawl, so ``memory/facts.py`` preserves them by
re-embedding), every vector here is derived from a document the module can read again. So the
collection is dropped, recreated at the new size, and the ledgers that claim its contents are
cleared, which makes the *next* walk re-embed everything from source.

Two things make that a shared object rather than a method on one indexer:

* The bundled platform docs and the per-module docs **share** ``<tenant>__docs``. A recreate
  driven by either one drops the other's points too, so both ledgers have to be cleared — a
  source that healed alone would leave the other claiming vectors that no longer exist, which
  is exactly the silent-hole failure this guard exists to prevent.
* Detection has to be free on the hot path. The guard caches the size it last confirmed, so
  every subsequent upsert in a pass costs one integer comparison — while still noticing a
  genuine switch, which the ``bool`` "already ensured" flag it replaces could not.

The heal ends by raising :class:`EmbeddingDimensionChanged`. The caller catches it and restarts
its pass from the top: by then the collection is empty and the ledgers are clear, so that
second pass rebuilds the whole source in one go instead of leaving a half-filled collection
behind for the next run to finish.

Detection cannot wait for the first upsert, because the pass that would notice may have
nothing to write: a corpus where every hash is unchanged embeds nothing, indexes nothing, and
would sail past a mismatch that has already broken *search* (a query embedded at the new width
against a collection built at the old one is rejected the same way). Module docs, which change
rarely, are almost always in exactly that state. So each pass over an existing collection pays
for one short **probe embed** up front and settles the question — one extra call per pass
against the whole corpus it is about to walk, and none at all on a fresh install, where the
first upsert creates the collection at the right size anyway. Caching the answer instead would
be cheaper and wrong: the switch this exists to catch happens *while the process is running*.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams

from epicurus_core import get_logger

_log = get_logger("knowledge.dimensions")


class EmbeddingDimensionChanged(RuntimeError):
    """The collection was recreated at a new vector size; the current pass must restart.

    Raised by :meth:`CollectionDimensionGuard.ensure` *after* the collection and every
    registered ledger have been reset, so a caller that simply retries its pass gets a clean
    full rebuild. It is not a failure: nothing is lost, and the retry is expected to succeed.
    """


class CollectionDimensionGuard:
    """Owns one Qdrant collection's vector size on behalf of every source that writes to it.

    Args:
        qdrant: The client owning ``collection``.
        collection: The tenant-scoped collection name (``<tenant>__docs``, …).

    Register each writing source's ledger reset with :meth:`register_reset` before use.
    """

    def __init__(self, qdrant: AsyncQdrantClient, collection: str) -> None:
        self._qdrant = qdrant
        self._collection = collection
        self._resets: list[Callable[[], Awaitable[None]]] = []
        self._dim: int | None = None
        self._lock = asyncio.Lock()

    @property
    def collection(self) -> str:
        return self._collection

    def register_reset(self, reset: Callable[[], Awaitable[None]]) -> None:
        """Register a ledger-clearing coroutine to run when the collection is recreated.

        Every source that upserts into this collection must register one, or a recreate will
        drop its vectors while its ledger still claims them.
        """
        self._resets.append(reset)

    def forget(self) -> None:
        """Drop the cached dimension — the collection may no longer exist or match.

        Called wherever a source deletes the collection itself (``reset``) or clears a ledger
        after finding it gone (``reconcile``).
        """
        self._dim = None

    async def _current_dim(self) -> int | None:
        """The live collection's vector size, or ``None`` if it can't be read as a single one.

        A named-vector configuration reports a mapping rather than one :class:`VectorParams`;
        we never create those, so treat anything unrecognised as "don't touch it" instead of
        guessing a size and dropping data on the guess.
        """
        info = await self._qdrant.get_collection(self._collection)
        vectors = info.config.params.vectors
        return vectors.size if isinstance(vectors, VectorParams) else None

    async def ensure(self, dim: int) -> None:
        """Make sure the collection exists at ``dim``, healing a drifted one.

        Raises :class:`EmbeddingDimensionChanged` when it had to recreate — see the module
        docstring for why that is a restart signal and not an error.
        """
        if self._dim == dim:
            return
        async with self._lock:
            if self._dim == dim:  # another task got there first
                return
            if not await self._qdrant.collection_exists(self._collection):
                await self._qdrant.create_collection(
                    self._collection,
                    vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
                )
                self._dim = dim
                return
            current = await self._current_dim()
            if current is None or current == dim:
                self._dim = dim
                return
            _log.warning(
                "embedding dimension changed since this collection was created; recreating it "
                "and clearing the ledgers so the next pass re-embeds from source",
                collection=self._collection,
                old_dim=current,
                new_dim=dim,
            )
            await self._qdrant.delete_collection(self._collection)
            await self._qdrant.create_collection(
                self._collection,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
            self._dim = dim
            for reset in self._resets:
                await reset()
        raise EmbeddingDimensionChanged(
            f"{self._collection} recreated at {dim} dimensions; restart the index pass"
        )
