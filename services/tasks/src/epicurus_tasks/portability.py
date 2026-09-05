"""Tenant data portability for tasks — the module's `PortabilityStore` (#867, #871).

Schema ``tasks/1``. Carries three kinds of record, each keyed by the module's own stable
id (never a surrogate autoincrement, per ADR-0133):

* ``task`` — a local task (``tasks_local``), keyed by its own uuid ``id``. Its in-row
  ``repeat`` (a local task's recurrence rule) travels as part of the record.
* ``task_repeat`` — the recurrence rule for an **external-provider** (Google) task
  (``task_repeats``), keyed by ``"<list_id>:<task_id>"``. Google Tasks has no recurrence
  field of its own, so this side table is the *only* place that rule lives (ADR-0082) —
  it travels even though the Google task it decorates does not.
* ``lead_time_prefs`` — the operator's `task_due_soon` lead time (``tasks_lead_time_prefs``),
  a single fixed-id record, exported only when the operator has set one explicitly.

Excluded, and why:

* **The Google task itself.** A connected Google task list lives in the operator's Google
  account, not this module's database — there is no mirror table to export. After import,
  the operator reconnects the account (as any OAuth-backed module requires) to see the
  tasks again; any already-imported ``task_repeat`` rows reattach to them by id the moment
  they do.
* **`tasks_fired_markers`** (``scheduler.py``) — operational fire-once dedup state for the
  lead-time scheduler (#664). Re-firing a `task_due_soon`/`task_overdue` notification once
  on the new install is harmless (the operator hasn't seen it there yet); carrying stale
  markers over would instead risk *suppressing* a real one.
* **Collection selection** (the operator's enabled Google lists + active list) is core-side
  state (`module_prefs.collections`, ADR-0030) already carried by the core's own export —
  duplicating it here would just be two copies of the same fact to keep in sync.

Import is an upsert by each kind's stable id; nothing here ever deletes. A second apply of
the same stream reports everything ``skipped``/``updated`` with nothing duplicated.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from epicurus_core import ImportOutcome, ImportReport, PortabilityRecord
from epicurus_tasks.db import RepeatStore, TaskStore
from epicurus_tasks.lead_time_prefs import LeadTimePrefsStore

SCHEMA = "tasks/1"

TASK_KIND = "task"
TASK_REPEAT_KIND = "task_repeat"
LEAD_TIME_PREFS_KIND = "lead_time_prefs"
_LEAD_TIME_PREFS_ID = "prefs"


def repeat_record_id(list_id: str, task_id: str) -> str:
    """A stable id for a ``task_repeat`` record: its own natural composite key."""
    return f"{list_id}:{task_id}"


def _parse_created_at(raw: object) -> datetime | None:
    """Best-effort ISO-8601 parse; ``None`` (fall back to the store's own default) on
    anything malformed rather than failing the whole import over one field."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class TasksPortability:
    """The tasks module's `PortabilityStore` (#867): tasks, external repeat rules, lead-time
    prefs."""

    schema = SCHEMA

    def __init__(
        self,
        *,
        tasks: TaskStore,
        repeats: RepeatStore,
        lead_prefs: LeadTimePrefsStore,
    ) -> None:
        self._tasks = tasks
        self._repeats = repeats
        self._lead_prefs = lead_prefs

    async def export(self, *, tenant_id: str) -> AsyncIterator[PortabilityRecord]:
        """Stream every source-of-truth record for *tenant_id* (see module docstring)."""
        async for task in self._tasks.export_tasks(tenant_id=tenant_id):
            task_id = task.pop("id")
            yield PortabilityRecord(kind=TASK_KIND, id=task_id, data=task)
        async for repeat in self._repeats.export_repeats(tenant_id=tenant_id):
            yield PortabilityRecord(
                kind=TASK_REPEAT_KIND,
                id=repeat_record_id(repeat["list_id"], repeat["task_id"]),
                data=dict(repeat),
            )
        lead_days = await self._lead_prefs.export_pref(tenant_id)
        if lead_days is not None:
            yield PortabilityRecord(
                kind=LEAD_TIME_PREFS_KIND,
                id=_LEAD_TIME_PREFS_ID,
                data={"lead_days": lead_days},
            )

    async def import_(
        self,
        *,
        tenant_id: str,
        records: AsyncIterator[PortabilityRecord],
        dry_run: bool,
    ) -> ImportReport:
        """Apply (or, with *dry_run*, only count) an incoming stream — upsert by stable id."""
        report = ImportReport(schema_name=self.schema)
        async for record in records:
            outcome: ImportOutcome
            if record.kind == TASK_KIND:
                outcome = await self._import_task(tenant_id, record, dry_run=dry_run)
            elif record.kind == TASK_REPEAT_KIND:
                outcome = await self._import_repeat(tenant_id, record, dry_run=dry_run)
            elif record.kind == LEAD_TIME_PREFS_KIND:
                outcome = await self._import_lead_time_prefs(tenant_id, record, dry_run=dry_run)
            else:
                report.warn(f"unknown record kind {record.kind!r}; skipped")
                outcome = "skipped"
            report.record(record.kind, outcome)
        return report

    async def _import_task(
        self, tenant_id: str, record: PortabilityRecord, *, dry_run: bool
    ) -> ImportOutcome:
        data: dict[str, Any] = record.data
        return await self._tasks.upsert_task(
            tenant_id=tenant_id,
            id=record.id,
            title=data.get("title") or "",
            notes=data.get("notes"),
            due=data.get("due"),
            completed=bool(data.get("completed", False)),
            completed_at=data.get("completed_at"),
            created_at=_parse_created_at(data.get("created_at")),
            status=data.get("status"),
            priority=data.get("priority"),
            tags=list(data.get("tags") or []),
            repeat=data.get("repeat"),
            dry_run=dry_run,
        )

    async def _import_repeat(
        self, tenant_id: str, record: PortabilityRecord, *, dry_run: bool
    ) -> ImportOutcome:
        data = record.data
        list_id = data.get("list_id")
        task_id = data.get("task_id")
        rrule = data.get("rrule")
        if not list_id or not task_id or not rrule:
            raise ValueError(f"malformed task_repeat record {record.id!r}: missing a field")
        return await self._repeats.upsert(
            tenant_id=tenant_id,
            list_id=list_id,
            task_id=task_id,
            rrule=rrule,
            dry_run=dry_run,
        )

    async def _import_lead_time_prefs(
        self, tenant_id: str, record: PortabilityRecord, *, dry_run: bool
    ) -> ImportOutcome:
        raw = record.data.get("lead_days")
        if not isinstance(raw, int):
            raise ValueError(f"malformed lead_time_prefs record {record.id!r}: bad lead_days")
        return await self._lead_prefs.upsert(tenant_id, raw, dry_run=dry_run)
