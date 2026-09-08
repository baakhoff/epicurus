"""The core's own half of a tenant archive — which tables travel, and how (#867).

The core owns ~40 tables. Only some of them are *the tenant's data*: the rest are either
**derived** (rebuildable from something that does travel) or **operational** (about this
running installation, meaningless in another one). Carrying the second kind would be worse
than useless — it would restore a queue of work that already ran, a kill switch someone
flipped last Tuesday, and push subscriptions pointing at a browser that will never see this
deployment. So the split is explicit here, table by table, and :data:`EXCLUSIONS` records
the reason for every omission in the archive itself.

The travelling tables are read and written **generically**, over SQLAlchemy's own column
metadata rather than each store's Python API. That is a deliberate trade: a bespoke
serializer per store would be forty hand-written round-trips to keep in step with forty
evolving models, and the first one to drift would lose data silently. Reading the columns
means a column added tomorrow travels tomorrow, with no edit here.

Two rules make that safe:

* **``tenant`` never travels.** It is stripped on export and re-applied from the *target*
  tenant on import, so an archive can never write into the tenant it came from by accident
  (constraint #1 — the archive is data, the tenant is context).
* **A surrogate key never travels.** An autoincrement ``id``/``pk`` means nothing in another
  database; each spec names the *natural* key that identifies the row across installations,
  and the upsert matches on that. This is what makes a second apply a no-op instead of a
  duplicate.
* **A ``NULL`` that the model has a default for never travels as ``NULL``** (#903). The
  additive reconcile (:mod:`epicurus_core.db`) adds a post-release column *nullable* when the
  model gives it no ``server_default``, because there is nothing to backfill a populated table
  with — so a long-lived source carries ``NULL`` in a column the model declares ``NOT NULL``,
  and its row-reader coerces that to the Python-side default on every read. A fresh target
  never went through that reconcile: ``create_all`` made the column ``NOT NULL``, and an
  explicit ``None`` in an ``insert()`` bypasses the ORM default and violates the constraint.
  Both ends therefore normalise here — :meth:`TableSpec.encode` on the way out and
  :meth:`TableSpec.normalize` on the way in — so an archive is portable regardless of which
  reconcile its source went through, and a null that *cannot* be defaulted costs one row
  rather than the whole set.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import (
    Boolean,
    Column,
    ColumnDefault,
    DateTime,
    LargeBinary,
    Table,
    UniqueConstraint,
    insert,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epicurus_core import ImportOutcome, ImportReport, PortabilityRecord
from epicurus_core_app.agent.instructions import (
    _AgentInstructionsRow,
    _AgentInstructionsVersionRow,
)
from epicurus_core_app.agent.playbook_review import _StoredDecision as _PlaybookDecisionRow
from epicurus_core_app.agent.playbook_review import _StoredProposal as _PlaybookProposalRow
from epicurus_core_app.agent.playbooks import _AgentPlaybookRow, _AgentPlaybookVersionRow
from epicurus_core_app.agent.session_model import _StoredSessionModel
from epicurus_core_app.automations.store import _StoredAutomation, _StoredAutomationSession
from epicurus_core_app.llm.model_settings import _ModelSettingsRow
from epicurus_core_app.llm.prefs import _LlmPrefRow
from epicurus_core_app.llm.saved_models import _SavedModelRow
from epicurus_core_app.maintenance_schedule_prefs import _MaintenanceScheduleRow
from epicurus_core_app.memory.profile import StoredProfile
from epicurus_core_app.memory.store import StoredAttachment, StoredMessage
from epicurus_core_app.module_prefs import _ModulePrefRow
from epicurus_core_app.notifications import _NotificationRow
from epicurus_core_app.page_order_prefs import _PageOrderRow
from epicurus_core_app.portability.models import ExclusionEntry
from epicurus_core_app.push.event_subscriptions import _EventSubscriptionRow
from epicurus_core_app.push.prefs import _PushPrefsRow
from epicurus_core_app.scheduled_turns import _StoredScheduledTurn
from epicurus_core_app.timezone_prefs import _TimezonePrefRow

__all__ = [
    "CORE_SCHEMA",
    "CORE_SETS",
    "EXCLUSIONS",
    "MEMORY_SET",
    "TableSpec",
    "export_set",
    "import_set",
]

CORE_SCHEMA = "core/1"
"""The record schema the core's own sets speak. Bumped when the *envelope* changes.

Not when a table gains a column: a column is additive on the wire (an older reader ignores
what it does not know, a newer one fills the default), which is exactly the property the
generic reader above buys. Reserved for a change that would make an old archive unreadable.
"""


@dataclass(frozen=True, slots=True)
class TableSpec:
    """One travelling table: its record ``kind`` and the natural key the upsert matches on.

    *key* names columns that identify the row **across installations**; an empty key means
    the table holds exactly one row per tenant (every prefs table), so the tenant *is* the
    key. *skip* names columns that must not travel — always the surrogate primary key, since
    its value is an artefact of the source database's insert order and nothing else.
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
        """A JSON-safe mapping of the travelling columns of *row*, nulls defaulted (#903)."""
        return {
            c.name: _encode_value(c, _defaulted(c, row[c.name]))
            for c in self.columns
            if c.name in row
        }

    def decode(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Python values for the travelling columns present in *data* (unknown keys dropped)."""
        by_name = {c.name: c for c in self.columns}
        return {
            name: _decode_value(by_name[name], value)
            for name, value in data.items()
            if name in by_name
        }

    def normalize(self, data: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
        """*data* with every fillable ``NULL`` replaced, plus the ones that could not be.

        The import-side twin of :meth:`encode`, and the reason an archive written *before*
        this rule existed still applies cleanly: the normalisation is what the reader does,
        not only what the writer did. Returns the JSON-side mapping (unknown keys kept — the
        caller still has to notice them) and the names of columns the model marks ``NOT NULL``
        that carry a ``NULL`` with no default to fill it. That second list is what turns a
        set-level ``IntegrityError`` into one skipped row with the column named.
        """
        by_name = {c.name: c for c in self.columns}
        normalized: dict[str, Any] = {}
        undefaultable: list[str] = []
        for name, value in data.items():
            column = by_name.get(name)
            if column is None:
                normalized[name] = value
                continue
            filled = _defaulted(column, value)
            if filled is None and not column.nullable:
                undefaultable.append(name)
            normalized[name] = filled
        return normalized, tuple(sorted(undefaultable))


def _table(model: Any) -> Table:
    """A mapped class's ``__table__``, narrowed (it is typed ``FromClause``, always a Table)."""
    return cast("Table", model.__table__)


# ── What travels ──────────────────────────────────────────────────────────────
#
# Grouped into the archive's ``core/<set>.ndjson`` members. The grouping is for the
# operator's benefit (an import preview that says "conversations: 4,812 records" reads;
# one that says "agent_messages, agent_attachments, session_models…" does not) — the
# records inside carry their own ``kind``, so a set is only ever a filename.

CORE_SETS: dict[str, tuple[TableSpec, ...]] = {
    # Chat: the messages themselves, the attachments they reference, and the per-session
    # model choice that makes reopening one behave the way it did.
    "conversations": (
        # No natural key exists on a message row, so the key is what actually identifies it:
        # its session, its instant, and its role. Two messages in one session sharing a
        # microsecond *and* a role is not a state the writer can produce (a turn appends
        # user then assistant), so this is an identity, not a heuristic.
        TableSpec(
            kind="agent_messages",
            table=_table(StoredMessage),
            key=("session_id", "created_at", "role"),
            skip=("id",),
        ),
        TableSpec(kind="agent_attachments", table=_table(StoredAttachment), key=("att_id",)),
        TableSpec(kind="session_models", table=_table(_StoredSessionModel), key=("session_id",)),
        # Which chat belongs to which automation (#672) — the chat list's grouping. Source of
        # truth, not operational: without it an imported automation's chats read as loose
        # user conversations.
        TableSpec(
            kind="automation_sessions",
            table=_table(_StoredAutomationSession),
            key=("session_id",),
        ),
    ),
    # How the assistant was taught to behave: the base prompt, the playbooks beside it, the
    # staged edits and the resolved decision trail the reflection pass reads back (ADR-0090).
    "agent": (
        TableSpec(kind="agent_instructions", table=_table(_AgentInstructionsRow)),
        TableSpec(
            kind="agent_instructions_versions",
            table=_table(_AgentInstructionsVersionRow),
            key=("vid",),
            skip=("id",),
        ),
        TableSpec(kind="agent_playbooks", table=_table(_AgentPlaybookRow), key=("id",)),
        TableSpec(
            kind="agent_playbook_versions",
            table=_table(_AgentPlaybookVersionRow),
            key=("vid",),
            skip=("id",),
        ),
        TableSpec(
            kind="agent_playbook_proposals",
            table=_table(_PlaybookProposalRow),
            key=("sid",),
            skip=("id",),
        ),
        TableSpec(
            kind="agent_playbook_decisions",
            table=_table(_PlaybookDecisionRow),
            key=("sid",),
            skip=("id",),
        ),
        # The standing profile is versioned per tenant with a surrogate id; its instant is
        # what distinguishes one synthesis from the next.
        TableSpec(
            kind="standing_profiles",
            table=_table(StoredProfile),
            key=("created_at",),
            skip=("id",),
        ),
    ),
    # What the assistant does unattended, and what it is subscribed to.
    "automations": (
        TableSpec(kind="automations", table=_table(_StoredAutomation), key=("id",), skip=("pk",)),
        TableSpec(
            kind="event_subscriptions",
            table=_table(_EventSubscriptionRow),
            key=("module", "event_type"),
            skip=("pk",),
        ),
        TableSpec(
            kind="scheduled_turns",
            table=_table(_StoredScheduledTurn),
            key=("id",),
            skip=("pk",),
        ),
    ),
    # The notification centre's durable record (#671) — the operator's own history, not a
    # delivery queue (``push_queue``/``push_subscriptions`` are excluded below).
    "notifications": (
        TableSpec(kind="notifications", table=_table(_NotificationRow), key=("id",), skip=("pk",)),
    ),
    # Every preference table: one row per tenant unless the preference is *about* something
    # (a model, a module), in which case that something is the key.
    "prefs": (
        TableSpec(kind="llm_prefs", table=_table(_LlmPrefRow)),
        TableSpec(kind="saved_models", table=_table(_SavedModelRow), key=("model",)),
        TableSpec(kind="model_settings", table=_table(_ModelSettingsRow), key=("model",)),
        TableSpec(kind="module_prefs", table=_table(_ModulePrefRow), key=("module",)),
        TableSpec(kind="timezone_prefs", table=_table(_TimezonePrefRow)),
        TableSpec(kind="page_order_prefs", table=_table(_PageOrderRow)),
        TableSpec(kind="push_prefs", table=_table(_PushPrefsRow)),
        TableSpec(kind="maintenance_schedule_prefs", table=_table(_MaintenanceScheduleRow)),
    ),
}

MEMORY_SET = "memory"
"""The one core set that is not a table: durable facts, read through the memory store.

They live in Qdrant, and the *points* are the wrong thing to carry — a vector is specific to
the embedding model that produced it, and the target installation may well run a different
one. The fact's text and metadata are the source of truth; the vector is derived, and the
import re-embeds it on the way in (see :mod:`.service`).
"""

EXCLUSIONS: tuple[ExclusionEntry, ...] = (
    ExclusionEntry(
        component="core_files",
        reason="derived — the file index is rebuilt by the forced rescan after import",
    ),
    ExclusionEntry(
        component="qdrant collections",
        reason="derived — vectors are model-specific; the re-embed fan-out rebuilds them",
    ),
    ExclusionEntry(
        component="module_events",
        reason="operational — the event log is this installation's record of what it saw",
    ),
    ExclusionEntry(
        component="automation_queue / automation_runs / automation_kill_switch",
        reason="operational — pending work, a run ledger, and a switch about this deployment",
    ),
    ExclusionEntry(
        component="automation_proposals / automation_review_decisions",
        reason="operational — a review queue awaiting a decision on the source installation",
    ),
    ExclusionEntry(
        component="agent_suspended_runs / agent_pending_drafts / agent_pending_approvals",
        reason="operational — turns paused mid-flight; there is nothing to resume here",
    ),
    ExclusionEntry(
        component="ephemeral_sessions",
        reason="operational — invisible chats are deleted on exit by design (#772)",
    ),
    ExclusionEntry(
        component="push_subscriptions / push_queue",
        reason="operational — device endpoints of the source deployment's browsers",
    ),
    ExclusionEntry(
        component="memory_extraction_queue / agent_reflection_state / maintenance_runs",
        reason="operational — background-job cursors and history",
    ),
    ExclusionEntry(
        component="secrets (provider API keys, OAuth tokens)",
        reason="never exported — held in OpenBao; the report names what to re-enter",
    ),
)
"""Every deliberate omission, with the reason, written into the archive's manifest.

Recorded rather than merely documented: an operator staring at a fresh import needs to know
that an empty Files search and a cold vector store are the *design*, and that the two
rebuilds the import runs are what fill them.
"""


def _canonical_dt(value: datetime) -> str:
    """A timezone-canonical ISO string, so a round trip through any dialect compares equal.

    SQLite has no timezone-aware type: an aware ``datetime`` written to it reads back naive,
    so the same row would encode differently before and after a round trip and the second
    apply of an archive would report *updated* where it must report *skipped*. Every instant
    the core writes is UTC, so a naive value is read as UTC and an aware one is converted to
    it — one canonical spelling on both sides of the trip.
    """
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat()


_NO_DEFAULT = object()
"""Sentinel — a column has no Python-side scalar default. ``None`` cannot say this: a
``default=None`` and a missing default are different facts, and so are ``default=False``
and no default at all."""


def _scalar_default(column: Column[Any]) -> Any:
    """*column*'s Python-side scalar default, or :data:`_NO_DEFAULT`.

    Only a plain value counts. A callable (``default=uuid4``) or a SQL expression
    (``server_default=func.now()``) is evaluated by the ``insert`` itself, which already
    happens for a column the record omits entirely — there is no value to write into a
    *record* here, and inventing one at export time would freeze one installation's clock
    into the archive.
    """
    default = column.default
    # ``ColumnDefault`` is the plain-value branch of SQLAlchemy's default hierarchy; a
    # ``CallableColumnDefault`` / ``Sequence`` has no ``arg`` to read, which is the same
    # answer as having none.
    if not isinstance(default, ColumnDefault) or not default.is_scalar:
        return _NO_DEFAULT
    return default.arg


def _defaulted(column: Column[Any], value: Any) -> Any:
    """*value*, with a ``NULL`` in a ``NOT NULL`` column replaced by the column's default.

    A **nullable** column is left alone: its ``NULL`` is data (``recurrence`` on a plain
    event, ``lead_minutes`` meaning "use the fallback"), and defaulting it would rewrite the
    operator's rows. Only a column the model declares ``NOT NULL`` can be carrying an
    impossible value, and only the reconcile (#249, ADR-0067) can have put it there.
    """
    if value is not None or column.nullable:
        return value
    default = _scalar_default(column)
    return None if default is _NO_DEFAULT else default


def _encode_value(column: Column[Any], value: Any) -> Any:
    """One column value, JSON-safe."""
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        return _canonical_dt(value) if isinstance(value, datetime) else str(value)
    if isinstance(column.type, LargeBinary):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(column.type, Boolean):
        return bool(value)
    return value


def _decode_value(column: Column[Any], value: Any) -> Any:
    """The inverse of :func:`_encode_value`, back to what the column wants."""
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        return datetime.fromisoformat(str(value)) if isinstance(value, str) else value
    if isinstance(column.type, LargeBinary):
        return base64.b64decode(str(value))
    if isinstance(column.type, Boolean):
        return bool(value)
    return value


async def export_set(
    engine: AsyncEngine, set_name: str, *, tenant: str
) -> AsyncIterator[PortabilityRecord]:
    """Stream one core set's records for *tenant*, table by table.

    Streams rather than collects: a long-lived install's ``agent_messages`` is the largest
    thing in the archive by a wide margin, and it is written straight into the tar.
    """
    for spec in CORE_SETS[set_name]:
        async with engine.connect() as conn:
            result = await conn.stream(select(spec.table).where(spec.table.c.tenant == tenant))
            async for row in result.mappings():
                data = spec.encode(dict(row))
                yield PortabilityRecord(kind=spec.kind, id=spec.identity(data), data=data)


async def import_set(
    engine: AsyncEngine,
    set_name: str,
    records: AsyncIterator[PortabilityRecord],
    *,
    tenant: str,
    dry_run: bool,
) -> ImportReport:
    """Upsert one core set's records into *tenant* — additively, never deleting.

    Per record: absent → inserted (``created``); present and identical → left alone
    (``skipped``); present and different → overwritten (``updated``). That third case is
    what makes an import *merge* rather than duplicate, and the second is what makes
    applying the same archive twice a no-op.

    A record the set cannot accept is a fourth case, and it costs **one row** (#903). The set
    runs in one transaction, so an exception raised mid-stream loses everything the set had
    already written — the operator's whole `prefs` set, for one bad `module_prefs` row. The
    two shapes that can do that are both answered before a statement is built: a kind this
    version has no table for, and a ``NULL`` in a ``NOT NULL`` column with no default to fill
    it. Both are ``skipped`` with a warning naming what was dropped.
    """
    by_kind = {spec.kind: spec for spec in CORE_SETS[set_name]}
    report = ImportReport(schema_name=CORE_SCHEMA)
    # One transaction for the whole set: a set either lands or does not, and a long
    # ``agent_messages`` stream is not paying for ten thousand round-trip commits.
    async with engine.begin() as conn:
        async for record in records:
            spec = by_kind.get(record.kind)
            if spec is None:
                # A newer source exported something this version has no table for. A fact to
                # report, not an error to fail on — the rest of the set still lands.
                report.record(record.kind, "skipped")
                report.warn(
                    f"unknown record kind {record.kind!r} in core set {set_name!r}; skipped"
                )
                continue
            outcome, unknown, warning = await _upsert(conn, spec, record, tenant, dry_run)
            report.record(record.kind, outcome)
            if unknown:
                report.warn(
                    f"{record.kind}: ignored unknown field(s) {sorted(unknown)} "
                    "written by a different schema"
                )
            if warning:
                report.warn(warning)
    return report


def _globally_unique(spec: TableSpec) -> bool:
    """Whether *spec*'s natural key is unique across the **whole table**, not per tenant.

    Several core tables key on a client-minted uuid (``att_id``, ``session_id``) or a
    generated id (``automations.id``) and enforce it globally rather than per tenant — the
    ids are unguessable, so a cross-tenant collision was unrepresentable by construction and
    the constraint was written the simple way. Which is true right up until an *import*
    tries to write another tenant's copy of the same row: the tenant-scoped lookup misses,
    the insert hits the global constraint, and the whole set fails on an IntegrityError.

    So the tables where that can happen are identified from their own metadata rather than
    listed by hand, and :func:`_upsert` looks again without the tenant filter before
    inserting into one. Derived, not hardcoded: a table that gains or loses the constraint
    changes this answer with it.
    """
    if not spec.key:
        return False
    keys = set(spec.key)
    primary = {c.name for c in spec.table.primary_key.columns}
    if primary and primary == keys:
        return True
    for constraint in spec.table.constraints:
        if (
            isinstance(constraint, UniqueConstraint)
            and {c.name for c in constraint.columns} == keys
        ):
            return True
    return len(keys) == 1 and bool(spec.table.c[spec.key[0]].unique)


async def _upsert(
    conn: AsyncConnection,
    spec: TableSpec,
    record: PortabilityRecord,
    tenant: str,
    dry_run: bool,
) -> tuple[ImportOutcome, set[str], str | None]:
    """Apply one record; return its outcome, unknown fields, and any warning it earned."""
    data, undefaultable = spec.normalize(record.data)
    values = spec.decode(data)
    unknown = set(record.data) - set(values)
    if undefaultable:
        # A null this version cannot fill. It would fail the target's NOT NULL constraint and
        # take the *whole set* with it (one transaction), so it is refused here, before the
        # statement is built: one row lost, named, and the other ten thousand still land.
        # Deliberately not naming the record id: warnings are de-duplicated by text, and a
        # source that lost a column lost it on every row — an id per line would put ten
        # thousand near-identical sentences in the report. The kind and the column say what
        # to look at; ``counts[kind].skipped`` says how many.
        return (
            "skipped",
            unknown,
            f"{record.kind}: no value for {', '.join(undefaultable)} and no default to fill "
            "it; the affected row(s) were skipped and the rest of the set still landed",
        )
    key_conditions = [spec.table.c[name] == values.get(name) for name in spec.key]
    conditions = [spec.table.c.tenant == tenant, *key_conditions]
    existing = (await conn.execute(select(spec.table).where(*conditions))).mappings().first()
    if existing is None:
        if _globally_unique(spec):
            owned = (
                (await conn.execute(select(spec.table).where(*key_conditions))).mappings().first()
            )
            if owned is not None:
                # The id is taken by another tenant. Import is additive and never destroys,
                # so this is a skip with an explanation — not a steal, and not a crash.
                return (
                    "skipped",
                    unknown,
                    f"{record.kind}: id {record.id!r} already belongs to another tenant; skipped",
                )
        if not dry_run:
            await conn.execute(insert(spec.table).values(tenant=tenant, **values))
        return "created", unknown, None
    encoded = spec.encode(dict(existing))
    # Compare only the columns this record actually carries: a column added after the
    # archive was written is absent here, and its default is not a difference to overwrite.
    # Against the *normalised* record, never the raw one: an archive's `null` and the target's
    # already-defaulted row are the same value, and reading them as a difference would report
    # `updated` on every re-apply of a pre-#903 archive.
    if all(encoded.get(name) == data[name] for name in values):
        return "skipped", unknown, None
    if not dry_run:
        await conn.execute(update(spec.table).where(*conditions).values(**values))
    return "updated", unknown, None
