"""Notes' half of the tenant portability contract (#872, part of #866; ADR-0133).

An operator lifting a tenant out of one epicurus and setting it down in another must find
their notes waiting for them — with their **bodies**. That single word is the whole design
decision here, and it goes the other way from the issue's starting sketch, so it is worth
stating plainly:

**The note body travels in this stream.** Postgres is the source of truth for a note's
content (:mod:`epicurus_notes.db`); the ``notes/<slug>.md`` file in the shared file space is
a *write-only mirror* (:mod:`epicurus_notes.mirror`) — derived output that nothing ever
reads back into the store. Carrying metadata only would have put every body in the archive's
``files/`` member and nowhere else, and no step of an import ever writes a file back into the
``notes`` table. The operator would have landed in a new install holding a Files tree full of
``.md`` and a Notes page full of empty documents. The mirror is a copy of the truth, not the
truth, and only the truth is portable.

That also settles the ordering question the issue asks about. The core applies an archive as
core sets → **modules** → ``files/`` → forced rescan → re-embed fan-out
(:mod:`epicurus_core_app.portability.service`), so a module's records land *before* the file
tree does. Because the row carried its own body, that ordering costs nothing: the note is
whole the moment this import returns. The mirror is reconciled afterwards from two directions
— the archive's own ``files/notes/<slug>.md`` in the file phase, and this module's startup
``NotesMirror.backfill()`` for anything still missing — and the vectors are rebuilt by the
re-embed fan-out calling ``POST /reindex``, which reads bodies straight out of the store.

What travels, and the stable id of each (never the surrogate ``id`` column, never ``tenant``):

============================  =====================================  ======================
kind                          stable id                              table
============================  =====================================  ======================
``note``                      the tenant-unique ``slug``             ``notes``
``note_folder``               the folder ``path``                    ``note_folders``
``note_suggestion``           the suggestion's own ``sid`` (uuid4)   ``notes_suggestions``
``note_suggestion_decision``  ``"<sid>:<decided_at ISO-8601 UTC>"``  ``notes_suggestion_decisions``
============================  =====================================  ======================

A decision row has no natural id of its own — ``sid`` names the *suggestion* it resolved —
so its id is derived deterministically from the pair that does identify it: the suggestion
and the moment it was resolved. ``sid`` is 32 hex characters, so the ``:`` separator splits
back unambiguously.

**Excluded, deliberately:**

* ``note_versions`` — per-save history (ADR-0046). Derived from edits rather than authored;
  deduped and pruned to the newest 50 per note, so it is already a lossy trace; and every row
  is a whole note body, which would multiply the archive by up to fifty to carry snapshots of
  text whose current state travels in full beside it. The head of that history *is* the note.
* The ``<tenant>__notes`` Qdrant collection — derived vectors, specific to the embedding
  model that produced them; rebuilt by the re-embed fan-out after an import (#332).
* The ``.md`` mirror — the core's file space carries it as a file, and this module would only
  be exporting the same bytes a second time.
* The review on/off toggle — a *core* preference (``/platform/v1/modules/notes/
  suggestions-enabled``), not this module's row; it travels in the archive's core sets.
* Everything in :class:`~epicurus_notes.settings.NotesSettings` — deployment configuration
  (URLs, chunk size), the new operator's to set, not the old one's to impose.

No secret material exists here to leak: notes holds no credentials at all (ADR-0010/0020).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from epicurus_core import ImportOutcome, ImportReport, PortabilityRecord, get_logger

# The ORM mappings, not the stores' CRUD. Portability is a *sibling* of the stores rather
# than a client of them: it needs whole rows — including columns no CRUD method returns
# (``created_at``, a suggestion's ``origin``) — and it writes rows verbatim rather than
# through methods that re-derive titles and stamp fresh timestamps.
from epicurus_notes.db import _StoredNote, _StoredNoteFolder
from epicurus_notes.suggestions import _StoredNoteDecision, _StoredNoteSuggestion

log = get_logger("notes.portability")

SCHEMA = "notes/1"
"""``<module>/<record schema version>`` — bumped only when a record's *shape* changes."""

NOTE = "note"
NOTE_FOLDER = "note_folder"
NOTE_SUGGESTION = "note_suggestion"
NOTE_SUGGESTION_DECISION = "note_suggestion_decision"


@dataclass(frozen=True)
class _Table:
    """One exported table: how a row becomes a record, and a record becomes a row.

    ``identity`` names the columns that compose the record's **stable id** (joined with
    ``:``); ``columns`` are the ones carried in ``data``. ``tenant`` and the surrogate ``id``
    appear in neither — the tenant is re-applied from the import's target tenant, and a
    surrogate key means nothing in another installation.
    """

    kind: str
    model: type[Any]
    identity: tuple[str, ...]
    columns: tuple[str, ...]
    dates: frozenset[str]
    # Columns the model cannot default: a *create* whose record omits one is refused rather
    # than written half-formed (see ``_apply``).
    required: frozenset[str]
    order_by: tuple[str, ...]


TABLES: tuple[_Table, ...] = (
    _Table(
        kind=NOTE,
        model=_StoredNote,
        identity=("slug",),
        columns=("title", "content", "created_at", "updated_at"),
        dates=frozenset({"created_at", "updated_at"}),
        # A note without a body is the one record this module refuses to invent (see the
        # module docstring): ``content`` is required to create one.
        required=frozenset({"title", "content"}),
        order_by=("slug",),
    ),
    _Table(
        kind=NOTE_FOLDER,
        model=_StoredNoteFolder,
        identity=("path",),
        columns=("created_at",),
        dates=frozenset({"created_at"}),
        required=frozenset(),
        order_by=("path",),
    ),
    _Table(
        kind=NOTE_SUGGESTION,
        model=_StoredNoteSuggestion,
        identity=("sid",),
        columns=("slug", "operation", "proposed_content", "origin", "note", "created_at"),
        dates=frozenset({"created_at"}),
        required=frozenset({"slug", "operation"}),
        order_by=("created_at", "id"),
    ),
    _Table(
        kind=NOTE_SUGGESTION_DECISION,
        model=_StoredNoteDecision,
        identity=("sid", "decided_at"),
        columns=(
            "slug",
            "operation",
            "origin",
            "note",
            "proposed_content",
            "applied_content",
            "decision",
            "proposed_at",
        ),
        dates=frozenset({"decided_at", "proposed_at"}),
        required=frozenset({"slug", "operation", "decision", "proposed_at"}),
        order_by=("decided_at", "id"),
    ),
)

_BY_KIND: dict[str, _Table] = {table.kind: table for table in TABLES}


def _iso(value: datetime | None) -> str | None:
    """A timestamp's canonical wire form: ISO-8601, always UTC, always offset-qualified.

    Naive values are read as UTC. SQLite's ``DATETIME`` storage carries no offset, so a
    round trip through it returns naive where Postgres returns aware — normalising both to
    the same string is what makes "did this row change?" answerable, and a second apply of
    the same archive a no-op rather than an endless stream of no-op updates.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _to_wire(table: _Table, column: str, value: Any) -> Any:
    """A row's column value as it travels: ISO-8601 for a timestamp, the value otherwise."""
    if column in table.dates:
        return _iso(value if isinstance(value, datetime) else None)
    return value


def _from_wire(table: _Table, column: str, value: Any) -> Any:
    """A wire value as the column wants it. Raises ``ValueError`` on a malformed timestamp."""
    if column in table.dates:
        if value is None or value == "":
            return None
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return "" if value is None else str(value)


def _normalize(table: _Table, column: str, value: Any) -> Any:
    """The wire value in canonical form — what a re-export of the written row would say."""
    return _to_wire(table, column, _from_wire(table, column, value))


def _row_data(table: _Table, row: Any) -> dict[str, Any]:
    """One row's ``data`` payload — its carried columns, canonical, minus tenant and key."""
    return {column: _to_wire(table, column, getattr(row, column)) for column in table.columns}


def _record_id(table: _Table, row: Any) -> str:
    """The row's stable id: its identity columns, canonical, joined with ``:``."""
    return ":".join(str(_to_wire(table, column, getattr(row, column))) for column in table.identity)


def _identity(table: _Table, record_id: str) -> dict[str, Any]:
    """Split a stable id back into its identity columns. Raises ``ValueError`` if malformed."""
    parts = record_id.split(":", len(table.identity) - 1)
    if len(parts) != len(table.identity) or not all(parts):
        raise ValueError(f"id {record_id!r} does not name a {table.kind}")
    return {
        column: _from_wire(table, column, part)
        for column, part in zip(table.identity, parts, strict=True)
    }


class NotesPortability:
    """The :class:`~epicurus_core.PortabilityStore` for notes — export and import in one.

    Holds the engine rather than the stores (see the import comment above): each half opens
    its own session, and an import applies as a **single transaction** — a stream that fails
    halfway leaves the tenant exactly as it was, which is the only honest reading of
    "additive, never destructive".
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    @property
    def schema(self) -> str:
        return SCHEMA

    async def export(self, *, tenant_id: str) -> AsyncIterator[PortabilityRecord]:
        """Stream this tenant's notes, folders, suggestions and decisions, in that order.

        Streamed row by row (``stream_scalars``), not collected: a note corpus is the one
        thing here that can be large, and the core writes each line straight into the archive.
        """
        for table in TABLES:
            async with self._session() as session:
                statement = (
                    select(table.model)
                    .where(table.model.tenant == tenant_id)
                    .order_by(*(getattr(table.model, column) for column in table.order_by))
                )
                rows = await session.stream_scalars(statement)
                async for row in rows:
                    yield PortabilityRecord(
                        kind=table.kind,
                        id=_record_id(table, row),
                        data=_row_data(table, row),
                    )

    async def import_(
        self,
        *,
        tenant_id: str,
        records: AsyncIterator[PortabilityRecord],
        dry_run: bool,
    ) -> ImportReport:
        """Upsert an incoming stream into *tenant_id*. Never deletes; a re-apply is a no-op."""
        report = ImportReport(schema_name=SCHEMA)
        # What a dry run *would* have written, so a second record for the same id inside one
        # stream is counted the way a real apply would count it. A real apply needs no such
        # bookkeeping: the session's autoflush makes its own earlier writes visible to the
        # very next lookup.
        staged: dict[tuple[str, str], dict[str, Any]] = {}
        async with self._session() as session:
            async for record in records:
                table = _BY_KIND.get(record.kind)
                if table is None:
                    report.record(record.kind, "skipped")
                    report.warn(
                        f"unknown record kind {record.kind!r}; skipped "
                        "(the archive was written by a notes that knows more than this one)"
                    )
                    continue
                try:
                    outcome = await self._apply(
                        session,
                        table,
                        record,
                        tenant_id=tenant_id,
                        dry_run=dry_run,
                        staged=staged,
                    )
                except ValueError as exc:
                    report.record(record.kind, "skipped")
                    report.warn(f"{record.kind} {record.id!r} skipped: {exc}")
                    continue
                report.record(record.kind, outcome)
            if not dry_run:
                await session.commit()
        log.info(
            "notes portability import",
            tenant=tenant_id,
            dry_run=dry_run,
            records=report.total,
        )
        return report

    async def _apply(
        self,
        session: AsyncSession,
        table: _Table,
        record: PortabilityRecord,
        *,
        tenant_id: str,
        dry_run: bool,
        staged: dict[tuple[str, str], dict[str, Any]],
    ) -> ImportOutcome:
        """Upsert one record; returns what it did (or, on a dry run, would have done).

        A key **absent** from ``data`` leaves that column alone — on an existing row it keeps
        what is there (so a metadata-only record can never blank a body it did not carry),
        and on a new row it falls back to the column's own default. The one thing that cannot
        fall back is a column the model has no default for: a create missing one of those is
        refused with a warning rather than written half-formed.
        """
        identity = _identity(table, record.id)
        present = {
            column: _normalize(table, column, record.data[column])
            for column in table.columns
            if column in record.data
        }
        key = (table.kind, record.id)
        current = staged.get(key) if dry_run else None
        row: Any = None
        if current is None:
            row = await self._find(session, table, identity, tenant_id=tenant_id)
            current = _row_data(table, row) if row is not None else None

        if current is None:
            missing = sorted(table.required - present.keys())
            if missing:
                raise ValueError(
                    f"cannot create it from this record — no {', '.join(missing)} in the archive"
                )
            if dry_run:
                staged[key] = dict(present)
            else:
                session.add(
                    table.model(
                        tenant=tenant_id,
                        **identity,
                        **{c: _from_wire(table, c, v) for c, v in present.items()},
                    )
                )
            return "created"

        merged = {**current, **present}
        if merged == current:
            if dry_run:
                staged[key] = merged
            return "skipped"
        if dry_run:
            staged[key] = merged
        else:
            assert row is not None  # a real apply always resolved ``current`` from a row
            for column, value in present.items():
                if current[column] != value:
                    setattr(row, column, _from_wire(table, column, value))
        return "updated"

    async def _find(
        self, session: AsyncSession, table: _Table, identity: dict[str, Any], *, tenant_id: str
    ) -> Any:
        """The existing row for this tenant + identity, or ``None``."""
        statement = select(table.model).where(table.model.tenant == tenant_id)
        for column, value in identity.items():
            statement = statement.where(getattr(table.model, column) == value)
        return await session.scalar(statement)


__all__ = [
    "NOTE",
    "NOTE_FOLDER",
    "NOTE_SUGGESTION",
    "NOTE_SUGGESTION_DECISION",
    "SCHEMA",
    "TABLES",
    "NotesPortability",
]
