"""Calendar's half of the tenant export/import contract (#870, part of #866).

An operator moving from one epicurus to another must arrive with their calendar intact.
Only two of this module's six tables are *the tenant's calendar*; the rest describe this
installation's relationship with Google, and carrying them would be worse than carrying
nothing — a restored sync cursor names a token the target's account never minted, and a
restored fired-marker would silence the first reminder the new install owed its operator.

**What travels**

* ``event`` — every row of ``calendar_events``: the local provider's own store, which is
  source of truth by definition (nothing else holds these events). A row is a plain event,
  a recurring **series master** (its RRULE and the wall-clock zone it expands in), or an
  **exception** overriding one occurrence — including a tombstoned one (#432). All three
  are ordinary rows here, so a series arrives with its exceptions still attached and the
  same occurrences expand on the far side.
* ``lead_time_prefs`` — the tenant's ``event_starting_soon`` lead time (#664). One row per
  tenant, so the record has no key of its own: the tenant *is* the key.

**What does not**

* ``calendar_fired_markers`` — operational. A marker records that *this* deployment already
  announced an event; the target has announced nothing.
* ``calendar_sync_state`` / ``calendar_synced_event`` / ``calendar_self_writes`` — a
  provider mirror and its cursors (#831). ``calendar_synced_event`` is a cache of what this
  module last *observed* in Google, not data anyone owns; the sync token is an opaque handle
  the source's own OAuth session minted. The next reconcile pass on the target rebuilds all
  three from the provider.
* **Google events themselves.** They live in Google Calendar, and the operator carries them
  by reconnecting the account — exporting a mirror would duplicate every event the moment
  the first sync ran.
* **The operator's calendar selection** (which calendars are enabled, which one new events
  land on). It is stored core-side in ``module_prefs`` (ADR-0030), and the *core's* archive
  already carries that table — duplicating it here would give one setting two owners.
* **Secrets.** This module holds none (ADR-0010/0020); the Google OAuth token stays in
  OpenBao and the core's import report names what to reconnect.

Both travelling tables are read and written over SQLAlchemy's own column metadata rather
than through ``LocalEventStore``'s API. That is deliberate: the store's methods are shaped
for the *provider* (partial edits, scope resolution, synthesized instances), and a
round-trip built on them would have to reconstruct a raw row from a resolved ``Event`` —
losing exactly the columns that make a series a series. Reading the columns also means a
column added tomorrow travels tomorrow, with no edit here.

Two rules make that safe, and they are the contract's (ADR-0133), not this module's:

* **``tenant`` never travels.** Stripped on export, re-applied from the *target* tenant on
  import — the archive is data, the tenant is context (constraint #1).
* **The surrogate ``id`` never travels.** An autoincrement pk means nothing in another
  database. ``event_id`` is the stable natural id (a uuid4 for a plain event or a master,
  ``<series>_<original start>`` for an exception — see :func:`~epicurus_calendar.db.instance_id`),
  and the upsert matches on it. That is what makes a second apply a no-op rather than a
  second copy of everybody's calendar.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Boolean, Column, DateTime, Table, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epicurus_calendar.db import _StoredEvent
from epicurus_calendar.lead_time_prefs import _LeadTimePrefRow
from epicurus_core import ImportOutcome, ImportReport, PortabilityRecord

__all__ = [
    "CALENDAR_SCHEMA",
    "EVENT_RECORD_KIND",
    "LEAD_TIME_RECORD_KIND",
    "CalendarPortability",
]

CALENDAR_SCHEMA = "calendar/1"
"""The record schema this module speaks — ``"<module>/<n>"``, not its release version.

Bumped only when the *envelope* changes in a way an older reader could not handle. A new
column is not that: it is additive on the wire (an older reader ignores what it does not
know, a newer one falls back to the column's default), which is precisely the property the
metadata-driven encode/decode below buys.
"""

EVENT_RECORD_KIND = "event"
"""One row of ``calendar_events`` — plain event, series master, or exception (#432)."""

LEAD_TIME_RECORD_KIND = "lead_time_prefs"
"""The tenant's ``event_starting_soon`` lead time (#664) — one record per tenant."""


@dataclass(frozen=True, slots=True)
class _TableSpec:
    """One travelling table: its record ``kind`` and the natural key the upsert matches on.

    *key* names the columns that identify a row **across installations**; an empty key means
    the table holds exactly one row per tenant, so the tenant is the key and the record id is
    just the kind. *skip* names columns that must not travel — the surrogate primary key,
    whose value is an artefact of the source database's insert order and nothing else.
    """

    kind: str
    table: Table
    key: tuple[str, ...] = ()
    skip: tuple[str, ...] = ()

    @property
    def columns(self) -> list[Column[Any]]:
        """The columns that travel: everything but ``tenant`` and the skipped surrogates."""
        return [c for c in self.table.columns if c.name != "tenant" and c.name not in self.skip]

    def identity(self, data: Mapping[str, Any]) -> str:
        """The record's stable id — the natural key's values, or the kind for a singleton."""
        if not self.key:
            return self.kind
        return "|".join(str(data.get(name)) for name in self.key)

    def encode(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """A JSON-safe mapping of the travelling columns of *row*."""
        return {c.name: _encode_value(c, row[c.name]) for c in self.columns if c.name in row}

    def decode(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Python values for the travelling columns present in *data* (unknown keys dropped)."""
        by_name = {c.name: c for c in self.columns}
        return {
            name: _decode_value(by_name[name], value)
            for name, value in data.items()
            if name in by_name
        }


def _table(model: Any) -> Table:
    """A mapped class's ``__table__``, narrowed (it is typed ``FromClause``, always a Table)."""
    return cast("Table", model.__table__)


_SPECS: tuple[_TableSpec, ...] = (
    # ``event_id`` is unique per tenant (``uq_calendar_tenant_event``) and stable across
    # installations — a uuid4 the module minted, or an instance id derived from one.
    _TableSpec(
        kind=EVENT_RECORD_KIND,
        table=_table(_StoredEvent),
        key=("event_id",),
        skip=("id",),
    ),
    # One row per tenant, keyed by the tenant itself — which never travels, so no key.
    _TableSpec(kind=LEAD_TIME_RECORD_KIND, table=_table(_LeadTimePrefRow)),
)


def _canonical_dt(value: datetime) -> str:
    """A timezone-canonical ISO string, so a round trip through any dialect compares equal.

    SQLite has no timezone-aware type: an aware ``datetime`` written to it reads back naive,
    so the same row would encode differently before and after a round trip and the second
    apply of an archive would report *updated* where it must report *skipped*. Every instant
    this module stores is UTC (``LocalEventStore._to_utc``), so a naive value is read as UTC
    and an aware one is converted to it — one canonical spelling on both sides of the trip.
    """
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat()


def _encode_value(column: Column[Any], value: Any) -> Any:
    """One column value, JSON-safe."""
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        return _canonical_dt(value) if isinstance(value, datetime) else str(value)
    if isinstance(column.type, Boolean):
        # SQLite hands back 0/1; the target may be Postgres, where the column is a real
        # boolean. Normalising here keeps an idempotent re-apply idempotent across dialects.
        return bool(value)
    return value


def _decode_value(column: Column[Any], value: Any) -> Any:
    """The inverse of :func:`_encode_value`, back to what the column wants."""
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        return datetime.fromisoformat(str(value)) if isinstance(value, str) else value
    if isinstance(column.type, Boolean):
        return bool(value)
    return value


class CalendarPortability:
    """Calendar's :class:`~epicurus_core.PortabilityStore` — served by ``add_portability_routes``.

    Holds only the engine: both routes are per-request and the service is stateless
    (constraint #2), so there is nothing to cache between an export and the import that
    follows it on another machine entirely.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @property
    def schema(self) -> str:
        """``"calendar/1"`` — the record schema, checked by the helper before a byte lands."""
        return CALENDAR_SCHEMA

    async def export(self, *, tenant_id: str) -> AsyncIterator[PortabilityRecord]:
        """Stream this tenant's events and lead-time preference as records.

        Streams rather than collects: a long-lived calendar is thousands of rows, and each
        line is written straight into the archive by the core.
        """
        for spec in _SPECS:
            async with self._engine.connect() as conn:
                statement = select(spec.table).where(spec.table.c.tenant == tenant_id)
                if spec.key:
                    # A stable order so two exports of an unchanged calendar are byte-identical
                    # — worth having when an operator diffs two archives to see what moved.
                    statement = statement.order_by(*[spec.table.c[name] for name in spec.key])
                result = await conn.stream(statement)
                async for row in result.mappings():
                    data = spec.encode(dict(row))
                    yield PortabilityRecord(kind=spec.kind, id=spec.identity(data), data=data)

    async def import_(
        self,
        *,
        tenant_id: str,
        records: AsyncIterator[PortabilityRecord],
        dry_run: bool,
    ) -> ImportReport:
        """Upsert an incoming stream into *tenant_id* — additively, never deleting.

        Per record: absent → inserted (``created``); present and identical → left alone
        (``skipped``); present and different → overwritten (``updated``). The third case is
        what makes an import *merge* into a populated calendar rather than duplicate it; the
        second is what makes applying the same archive twice a no-op.

        Rows are independent — an exception carries its series in its own ``event_id`` rather
        than a foreign key — so no ordering is required of the stream, and a partially
        readable archive still lands everything it could read.
        """
        by_kind = {spec.kind: spec for spec in _SPECS}
        report = ImportReport(schema_name=CALENDAR_SCHEMA)
        # One transaction for the whole stream: a calendar either lands or does not, and a
        # few thousand events are not paying for a round-trip commit each.
        async with self._engine.begin() as conn:
            async for record in records:
                spec = by_kind.get(record.kind)
                if spec is None:
                    # A newer calendar exported something this version has no table for. A
                    # fact to report, not an error to fail on — the rest of the stream lands.
                    report.record(record.kind, "skipped")
                    report.warn(f"unknown record kind {record.kind!r}; skipped")
                    continue
                outcome, unknown = await _upsert(conn, spec, record, tenant_id, dry_run)
                report.record(record.kind, outcome)
                if unknown:
                    report.warn(
                        f"{record.kind}: ignored unknown field(s) {sorted(unknown)} "
                        "written by a different schema"
                    )
        return report


async def _upsert(
    conn: AsyncConnection,
    spec: _TableSpec,
    record: PortabilityRecord,
    tenant: str,
    dry_run: bool,
) -> tuple[ImportOutcome, set[str]]:
    """Apply one record; return its outcome and any fields this version does not know.

    Every lookup is tenant-scoped, and both tables' uniqueness is per tenant
    (``uq_calendar_tenant_event``; the prefs table's tenant primary key), so one tenant's
    import can never collide with — or reach — another's rows.
    """
    values = spec.decode(record.data)
    unknown = set(record.data) - set(values)
    conditions = [
        spec.table.c.tenant == tenant,
        *[spec.table.c[name] == values.get(name) for name in spec.key],
    ]
    existing = (await conn.execute(select(spec.table).where(*conditions))).mappings().first()
    if existing is None:
        if not dry_run:
            await conn.execute(insert(spec.table).values(tenant=tenant, **values))
        return "created", unknown
    encoded = spec.encode(dict(existing))
    # Compare only the columns this record actually carries: a column added after the archive
    # was written is absent here, and its default is not a difference worth overwriting.
    if all(encoded.get(name) == record.data[name] for name in values):
        return "skipped", unknown
    if not dry_run:
        await conn.execute(update(spec.table).where(*conditions).values(**values))
    return "updated", unknown
