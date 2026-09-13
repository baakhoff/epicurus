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
column added tomorrow travels tomorrow, with no edit here. The encode/decode/normalize
machinery that buys that is :class:`~epicurus_core.PortableTable` (promoted out of here and
the core's own ``core_data`` in #918 — see its docstring for the shared rule) — this module
supplies only the *table specs* and the upsert loop around them.

Three rules make that safe, and they are the contract's (ADR-0133), not this module's:

* **``tenant`` never travels.** Stripped on export, re-applied from the *target* tenant on
  import — the archive is data, the tenant is context (constraint #1).
* **The surrogate ``id`` never travels.** An autoincrement pk means nothing in another
  database. ``event_id`` is the stable natural id (a uuid4 for a plain event or a master,
  ``<series>_<original start>`` for an exception — see :func:`~epicurus_calendar.db.instance_id`),
  and the upsert matches on it. That is what makes a second apply a no-op rather than a
  second copy of everybody's calendar.
* **A ``NULL`` the model has a default for never travels as ``NULL``** (#903). ``all_day``
  and ``excluded`` postdate this table's first release and carry no ``server_default``, so
  the additive reconcile added them nullable on every install provisioned before them; a
  fresh target's ``create_all`` makes them ``NOT NULL``. Both ends normalise
  (:class:`~epicurus_core.PortableTable`), so an archive from a reconciled source lands on a
  fresh schema instead of 500-ing the import and taking the whole calendar with it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epicurus_calendar.db import _StoredEvent
from epicurus_calendar.lead_time_prefs import _LeadTimePrefRow
from epicurus_core import ImportOutcome, ImportReport, PortabilityRecord, PortableTable, table_of

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


_SPECS: tuple[PortableTable, ...] = (
    # ``event_id`` is unique per tenant (``uq_calendar_tenant_event``) and stable across
    # installations — a uuid4 the module minted, or an instance id derived from one.
    PortableTable(
        kind=EVENT_RECORD_KIND,
        table=table_of(_StoredEvent),
        key=("event_id",),
        skip=("id",),
    ),
    # One row per tenant, keyed by the tenant itself — which never travels, so no key.
    PortableTable(kind=LEAD_TIME_RECORD_KIND, table=table_of(_LeadTimePrefRow)),
)


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
                outcome, unknown, warning = await _upsert(conn, spec, record, tenant_id, dry_run)
                report.record(record.kind, outcome)
                if unknown:
                    report.warn(
                        f"{record.kind}: ignored unknown field(s) {sorted(unknown)} "
                        "written by a different schema"
                    )
                if warning:
                    report.warn(warning)
        return report


async def _upsert(
    conn: AsyncConnection,
    spec: PortableTable,
    record: PortabilityRecord,
    tenant: str,
    dry_run: bool,
) -> tuple[ImportOutcome, set[str], str | None]:
    """Apply one record; return its outcome, unknown fields, and any warning it earned.

    Every lookup is tenant-scoped, and both tables' uniqueness is per tenant
    (``uq_calendar_tenant_event``; the prefs table's tenant primary key), so one tenant's
    import can never collide with — or reach — another's rows.
    """
    data, undefaultable = spec.normalize(record.data)
    values = spec.decode(data)
    unknown = set(record.data) - set(values)
    if undefaultable:
        # A null this version cannot fill. It would fail the target's NOT NULL constraint and
        # take the whole stream with it (one transaction), so it is refused before the
        # statement is built: one event lost, named, and the rest of the calendar still lands.
        # No record id in the sentence — warnings de-duplicate by text, and a source that lost
        # a column lost it on every row (#903).
        return (
            "skipped",
            unknown,
            f"{record.kind}: no value for {', '.join(undefaultable)} and no default to fill "
            "it; the affected row(s) were skipped and the rest of the stream still landed",
        )
    conditions = [
        spec.table.c.tenant == tenant,
        *[spec.table.c[name] == values.get(name) for name in spec.key],
    ]
    existing = (await conn.execute(select(spec.table).where(*conditions))).mappings().first()
    if existing is None:
        if not dry_run:
            await conn.execute(insert(spec.table).values(tenant=tenant, **values))
        return "created", unknown, None
    encoded = spec.encode(dict(existing))
    # Compare only the columns this record actually carries: a column added after the archive
    # was written is absent here, and its default is not a difference worth overwriting.
    # Against the *normalised* record, never the raw one: an archive's `null` and the target's
    # already-defaulted row are the same value, and reading them as a difference would report
    # `updated` on every re-apply of an archive written before #903.
    if all(encoded.get(name) == data[name] for name in values):
        return "skipped", unknown, None
    if not dry_run:
        await conn.execute(update(spec.table).where(*conditions).values(**values))
    return "updated", unknown, None
