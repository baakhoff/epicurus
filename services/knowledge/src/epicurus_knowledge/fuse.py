"""The mass de-index fuse — a vanished source must never purge derived state (#848).

Every indexing pass in this module is a *reconciler*: it walks a source (the vault behind
the core file API, the bundled docs tree, a module's ``/module-docs``) and deletes the
ledger rows and Qdrant vectors the source no longer has. That is correct when the source
is telling the truth, and catastrophic when it isn't — the 2026-08-30 dogfood incident
mounted an empty directory where the vault lives, and the pass dutifully reconciled a full
knowledge base down to nothing without a single error, because "the vault reads empty" is a
legitimate state (:class:`~epicurus_knowledge.reader.VaultReader` reports an absent vault as
empty, never as a failure).

The fuse restores the missing distinction between *the source shrank* and *the source
vanished*. Before a pass deletes anything it compares the deletion against what is already
indexed, and refuses the pass outright when the deletion is anomalously large:

* the source reads **empty** while the ledger is non-empty — always refused, at any size;
* otherwise the deletion is refused when it covers at least ``max_delete_ratio`` of the
  ledger **and** at least ``min_deletions`` rows (the floor keeps ordinary editing — two
  notes deleted out of three — from tripping a safety valve meant for wholesale loss).

A refusal leaves the ledger and the collection exactly as they were, logs loudly with the
numbers, and stays visible on ``GET /status`` and ``/metrics`` until a pass completes
cleanly. Deliberate wholesale deletion is still possible — ``force=true`` on ``POST
/reindex`` and on the ``knowledge_reindex`` tool bypasses the fuse for that pass.

Everything here is tenant-scoped (constraint #1): a fuse instance belongs to one tenant and
one source, and every metric carries both as labels.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from prometheus_client import Counter, Gauge

from epicurus_core import get_logger

log = get_logger("knowledge.fuse")

# 1 while a source's fuse is tripped, back to 0 once a pass completes without tripping it.
FUSE_TRIPPED = Gauge(
    "epicurus_knowledge_index_fuse_tripped",
    "1 while a knowledge index source is refusing to de-index (mass de-index fuse).",
    ["tenant", "source"],
)
# Monotonic history — a fuse that trips, is forced, and trips again still shows up here.
FUSE_TRIPS = Counter(
    "epicurus_knowledge_index_fuse_trips_total",
    "Knowledge index passes refused by the mass de-index fuse.",
    ["tenant", "source"],
)


@dataclass(frozen=True, slots=True)
class FusePolicy:
    """Thresholds for one fuse — see :data:`KnowledgeSettings` for the config keys.

    Args:
        enabled: Master switch. Disabled, :meth:`IndexFuse.evaluate` never trips (the
            pre-#848 behaviour) — an escape hatch for an operator whose source legitimately
            churns wholesale, not a default worth choosing.
        max_delete_ratio: Share of the ledger whose deletion is treated as suspect
            (``0.5`` = half). Compared with ``>=``.
        min_deletions: Absolute floor below which the ratio rule never fires, so a tiny
            knowledge base (a 3-note vault losing 2) is not permanently un-prunable. The
            empty-source rule ignores this floor: a source that reads *entirely* empty is
            the stale-mount signature at any size, and is refused even for a one-note
            ledger (delete it with ``force=true``).
    """

    enabled: bool = True
    max_delete_ratio: float = 0.5
    min_deletions: int = 5


@dataclass(frozen=True, slots=True)
class FuseTrip:
    """A refused pass: what would have been deleted, and why it was not."""

    tenant: str
    source: str
    ledger_rows: int
    would_delete: int
    source_entries: int
    reason: str
    at: str

    def as_dict(self) -> dict[str, object]:
        """The trip as a JSON-safe mapping (logs, ``GET /status`` detail)."""
        return {
            "tenant": self.tenant,
            "source": self.source,
            "ledger_rows": self.ledger_rows,
            "would_delete": self.would_delete,
            "source_entries": self.source_entries,
            "reason": self.reason,
            "at": self.at,
        }

    def summary(self) -> str:
        """One flat human sentence — the module status panel renders scalars only."""
        return f"{self.source}: {self.reason} (at {self.at})"


class IndexFuse:
    """The fuse for one ``(tenant, source)`` pair — policy plus current tripped state.

    Held by the indexer that owns the source, so the decision is taken where the deletion
    would happen and the state is readable from the app (``GET /status``) without a
    registry. ``source`` is the collection base name (``knowledge`` / ``docs`` /
    ``module_docs``), which is what an operator sees in the logs and metrics.
    """

    def __init__(self, *, tenant: str, source: str, policy: FusePolicy | None = None) -> None:
        self._tenant = tenant
        self._source = source
        self._policy = policy or FusePolicy()
        self._trip: FuseTrip | None = None
        # Publish the un-tripped state up front so the series exists before the first pass.
        FUSE_TRIPPED.labels(tenant=tenant, source=source).set(0)

    @property
    def policy(self) -> FusePolicy:
        return self._policy

    @property
    def tripped(self) -> bool:
        """Whether the most recent pass was refused (cleared by the next clean pass)."""
        return self._trip is not None

    @property
    def last_trip(self) -> FuseTrip | None:
        return self._trip

    def evaluate(
        self,
        *,
        ledger_rows: int,
        would_delete: int,
        source_entries: int,
        force: bool = False,
        empty_source_trips: bool = True,
    ) -> FuseTrip | None:
        """Judge one prospective pass; ``None`` means "go ahead".

        Pure and side-effect-free so a caller can ask the question without committing to
        it (the ``POST /reindex`` pre-check does exactly that). :meth:`trip` records the
        verdict when the caller acts on it.

        Args:
            ledger_rows: Rows already indexed for this tenant + source. ``0`` is the
                first-ever index — there is nothing to protect, so it never trips.
            would_delete: How many of those rows this pass would remove.
            source_entries: How many entries the source currently reports. ``0`` with a
                non-empty ledger is the wholesale-disappearance signature.
            force: The caller explicitly asked for the deletion — never trips.
            empty_source_trips: Whether an empty source escalates past the ``min_deletions``
                floor. True for a file tree, where "the directory is there and holds nothing"
                is the stale-mount fingerprint. False for the module registry, where listing
                no doc-serving module is an ordinary state — a single-module install
                disabling that module — and the ratio rule alone is the right guard.
        """
        if force or not self._policy.enabled:
            return None
        if ledger_rows <= 0 or would_delete <= 0:
            return None
        if source_entries <= 0 and empty_source_trips:
            return self._make(
                ledger_rows,
                would_delete,
                source_entries,
                f"source read empty while {ledger_rows} entr"
                f"{'y is' if ledger_rows == 1 else 'ies are'} indexed; "
                "refusing to de-index (suspect mount or unprovisioned source)",
            )
        if would_delete < self._policy.min_deletions:
            return None
        ratio = would_delete / ledger_rows
        if ratio < self._policy.max_delete_ratio:
            return None
        return self._make(
            ledger_rows,
            would_delete,
            source_entries,
            f"pass would delete {would_delete} of {ledger_rows} indexed entries "
            f"({ratio:.0%} >= {self._policy.max_delete_ratio:.0%} threshold); "
            "refusing to de-index",
        )

    def _make(self, ledger_rows: int, would_delete: int, entries: int, reason: str) -> FuseTrip:
        return FuseTrip(
            tenant=self._tenant,
            source=self._source,
            ledger_rows=ledger_rows,
            would_delete=would_delete,
            source_entries=entries,
            reason=reason,
            at=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    def trip(self, trip: FuseTrip) -> None:
        """Record a refusal: loud structured log, metrics, and the state ``/status`` reads."""
        self._trip = trip
        FUSE_TRIPPED.labels(tenant=self._tenant, source=self._source).set(1)
        FUSE_TRIPS.labels(tenant=self._tenant, source=self._source).inc()
        log.error("mass de-index fuse tripped; index pass refused", **trip.as_dict())

    def clear(self) -> None:
        """Re-arm after a pass that completed without tripping."""
        if self._trip is not None:
            log.info(
                "mass de-index fuse cleared; source reads normally again",
                tenant=self._tenant,
                source=self._source,
            )
        self._trip = None
        FUSE_TRIPPED.labels(tenant=self._tenant, source=self._source).set(0)


class FusedSource(Protocol):
    """An indexer that owns a fuse and can be asked about a prospective rebuild."""

    fuse: IndexFuse

    async def check_source_fuse(self, *, force: bool = False) -> FuseTrip | None: ...


async def rebuild_refusals(sources: Sequence[FusedSource]) -> list[FuseTrip]:
    """Ask every source whether a from-scratch rebuild would be a mass de-index (#848).

    The guard in front of ``POST /reindex``. A rebuild ``reset``s each ledger before walking,
    so by the time the per-pass fuse runs there is nothing left for it to protect — the
    question has to be asked *first*, on the state a reset is about to discard. Every refusal
    is recorded on its own source's fuse, so it reaches ``GET /status`` and ``/metrics`` and
    not just the log; an empty list means every source is safe to rebuild.
    """
    trips: list[FuseTrip] = []
    for source in sources:
        trip = await source.check_source_fuse()
        if trip is not None:
            source.fuse.trip(trip)
            trips.append(trip)
    return trips
