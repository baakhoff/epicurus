"""Collection router — fans task reads/writes across the operator's selection (ADR-0030/0036).

The router holds the always-present local store plus each external provider (Google) and
routes per the operator's stored selection, fetched from the core via a
:class:`CollectionPrefsSource` (the module's ``PlatformClient``). Tasks is a ``multi``
module — **each enabled list is a category**:

* **reads** (``list_tasks`` with no ``list_id``) aggregate every *enabled* list, tagging
  each task with the list it came from (``list_id`` / ``list_title``); a failing source is
  skipped, not fatal (#209). An explicit ``list_id`` reads just that one list.
* **creates** (``add_task``) target the list named by ``list_id``; with none, the default
  write target is the *active* list, else the first enabled, else local.
* **per-task mutations** (``complete_task`` / ``update_task`` / ``delete_task``) target the
  list named by ``list_id`` when given; with none, they *search* for the task the same way
  ``get_task`` does (active → other enabled → local) instead of assuming the default write
  target, so a mutation reaches a task living in a non-default list (#475).
* ``get_task`` searches the active, then the other enabled, then local — so a referenced
  task resolves wherever it lives.
* ``create_list`` routes to the sole configured external provider (Google, today) — the
  local store has no lists of its own to create (ADR-0030, #474).

It satisfies the :class:`~epicurus_tasks.providers.TasksProvider` Protocol, so the module's
tools and board treat it like any other backend. Reads fall back to the local store when
nothing is enabled or the core is unreachable (local-first).

``add_task``/``complete_task``/``update_task``/``_move_task`` are also the module-event-spine
emission seam (#664, ADR-0103): ``task_created``/``task_completed``/``task_updated``/
``task_moved`` fire here, the one place every operator/agent-driven write already passes
through regardless of which backend handles it. A task materialized by the recurrence sweep
(``_materialize``, called from both ``_materialize_next`` and ``_sweep_overdue``) calls the
*inner* provider's ``add_task`` directly, bypassing this router's own ``add_task`` — so a
recurring task's auto-spawned successor does **not** currently emit ``task_created``. Flagged
as a known, deliberate scope limit for this PR, not an oversight.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, tzinfo
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from epicurus_core import (
    LOCAL_ACCOUNT,
    Collection,
    CollectionPrefs,
    CollectionRef,
    EntityRef,
    EventBus,
    emit_event,
    get_logger,
)
from epicurus_tasks.models import Task, TaskScope
from epicurus_tasks.providers import TasksProvider
from epicurus_tasks.recurrence import next_due

log = get_logger("epicurus_tasks.router")

_LOCAL_REF = CollectionRef(account=LOCAL_ACCOUNT)
_LOCAL_TITLE = "Personal"
"""Category label for the silent local default list (ADR-0036)."""


async def _utc_today() -> str:
    """Today's date (UTC) as an ISO string — the clock default when no operator timezone
    source is wired (#535); production wires :func:`operator_clock` instead."""
    return datetime.now(UTC).date().isoformat()


TimezoneSource = Callable[[], Awaitable[str]]
"""Returns the operator's configured IANA timezone (the core's, ADR-0039) — the same seam
calendar's `_resolve_timezone` reads (#433), applied here to the recurrence clock (#535)."""


async def _resolve_timezone(source: TimezoneSource | None) -> tzinfo:
    """The zone the recurrence clock reads "today" in — the operator's, else UTC (#535).

    Best-effort by design, mirroring calendar's `_resolve_timezone` (#433): an unreachable
    core or an unknown zone name degrades to UTC (the pre-#535 behaviour) rather than
    failing the read or materialization that depends on it.
    """
    if source is None:
        return UTC
    try:
        return ZoneInfo(await source())
    except Exception as exc:
        log.warning("operator timezone unavailable; recurrence clock stays UTC", error=str(exc))
        return UTC


_TZ_MEMO_TTL_SECONDS = 60.0
"""How long `operator_clock` reuses a resolved timezone before re-fetching it (#553). A single
read already resolves "today" once (see `TasksRouter.list_tasks`); this memo additionally
collapses the fetch *across* reads in the same window — the board render and any chat-turn reads
seconds apart share one `get_timezone` round trip rather than each paying its own — and caps
core-down warnings to one per window instead of one per call. 60 s keeps a timezone change the
operator makes visible within a minute."""


def operator_clock(
    source: TimezoneSource,
    *,
    ttl_seconds: float = _TZ_MEMO_TTL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> Callable[[], Awaitable[str]]:
    """Builds a `now` callable for :class:`TasksRouter` using the operator's timezone (#535).

    Without this, the overdue sweep and materialization compute "today" in UTC regardless of
    where the operator actually is — a UTC-negative operator had a task swept (rule retired,
    successor spawned) up to the offset hours early; UTC-positive, late. The sweep made this
    automatic rather than user-triggered (#515), so the skew now acts on its own instead of
    being a one-off surprise on a manual action.

    The resolved zone is memoized for *ttl_seconds* (#553): ``PlatformClient.get_timezone`` is a
    fresh HTTP round trip per call, and this clock is read on every task read (the sweep) and the
    board render (#555), so an uncached clock cost one core call per read — C calls for C enabled
    collections — plus a core-down warning each. The memo is best-effort (a concurrent miss may
    fetch twice — harmless) and takes injectable *monotonic*/*ttl_seconds* so tests are
    deterministic without sleeping.
    """
    cached_zone: tzinfo | None = None
    expires_at = 0.0

    async def _today() -> str:
        nonlocal cached_zone, expires_at
        clock = monotonic()
        if cached_zone is None or clock >= expires_at:
            cached_zone = await _resolve_timezone(source)
            expires_at = clock + ttl_seconds
        return datetime.now(cached_zone).date().isoformat()

    return _today


class CollectionPrefsSource(Protocol):
    """Returns the operator's stored collection selection (the module's PlatformClient)."""

    async def get_collections(self) -> CollectionPrefs: ...


def _sort_key(task: Task) -> tuple[int, str, str]:
    """Deterministic cross-list order: dated tasks first (by date), then undated, by title."""
    return (0, task.due[:10], task.title) if task.due else (1, "", task.title)


class TasksRouter:
    """Routes task ops across local + external providers per the operator's selection."""

    def __init__(
        self,
        *,
        local: TasksProvider,
        external: dict[str, TasksProvider],
        prefs: CollectionPrefsSource,
        now: Callable[[], Awaitable[str]] = _utc_today,
        bus: EventBus | None = None,
    ) -> None:
        self._local = local
        self._external = external
        self._prefs = prefs
        self._bus = bus
        # Clock for recurrence materialization (ADR-0082); injectable so tests are
        # deterministic without freezing the wall clock. Defaults to UTC; production wires
        # `operator_clock(platform.get_timezone)` instead (#535).
        self._now = now
        # In-process guard against double-materializing the same anchor (#533) — see
        # `_claim_materialize`/`_release_materialize`. Best-effort only: it is per-process
        # memory, not shared/persisted state, so it narrows the race within one running
        # instance rather than guaranteeing it across replicas. That is a deliberate scope
        # limit (flagged for review), not an oversight — see the methods' docstrings.
        self._materializing: set[tuple[str, str]] = set()

    def provider_name(self) -> str:
        return "tasks"

    def _provider_for(self, account: str) -> TasksProvider | None:
        """The provider backing *account*, or ``None`` if it isn't configured/connected."""
        if account == LOCAL_ACCOUNT:
            return self._local
        return self._external.get(account)

    async def list_tasks(
        self, tenant_id: str, *, list_id: str | None = None, scope: TaskScope = "open"
    ) -> list[Task]:
        prefs = await self._load_prefs()
        # Resolve "today" once per read, before any materialize claim (#553): the overdue sweep
        # runs per enabled collection and each occurrence it materializes would otherwise re-fetch
        # the operator timezone (C collections → C core calls, plus a fetch per anchor *inside* the
        # claimed window). One resolution threaded down means one core call per read, a smaller
        # claimed window (pure DB work), and one "today" the sweep and _materialize can't disagree
        # on across a midnight tick.
        today = await self._now()
        if list_id is not None:
            target, ref = self._resolve_collection(list_id, prefs)
            tasks = await target.list_tasks(tenant_id, list_id=ref.collection or None, scope=scope)
            tasks = await self._sweep_overdue(tenant_id, target, ref, tasks, today=today)
            titles = await self._title_map(tenant_id, [ref])
            return self._stamp(tasks, ref=ref, title=titles.get((ref.account, ref.collection)))
        targets = prefs.enabled or [_LOCAL_REF]
        titles = await self._title_map(tenant_id, targets)
        out: list[Task] = []
        for ref in targets:
            provider = self._provider_for(ref.account)
            if provider is None:
                continue  # an unknown / disconnected account is skipped, not fatal
            try:
                tasks = await provider.list_tasks(
                    tenant_id, list_id=ref.collection or None, scope=scope
                )
            except Exception as exc:
                log.warning(
                    "tasks read failed; skipping this source (#209)",
                    account=ref.account,
                    collection=ref.collection,
                    error=str(exc),
                )
                continue
            tasks = await self._sweep_overdue(tenant_id, provider, ref, tasks, today=today)
            out.extend(self._stamp(tasks, ref=ref, title=titles.get((ref.account, ref.collection))))
        out.sort(key=_sort_key)
        return out

    async def add_task(
        self,
        tenant_id: str,
        title: str,
        *,
        notes: str | None = None,
        due: str | None = None,
        status: str = "open",
        priority: str | None = None,
        tags: list[str] | None = None,
        list_id: str | None = None,
        repeat: str | None = None,
    ) -> Task:
        prefs = await self._load_prefs()
        provider, ref = self._resolve_collection(list_id, prefs)
        task = await provider.add_task(
            tenant_id,
            title,
            notes=notes,
            due=due,
            status=status,
            priority=priority,
            tags=tags,
            list_id=ref.collection or None,
            repeat=repeat,
        )
        await self._emit_created(tenant_id, task, ref)
        return task

    async def complete_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> Task:
        prefs = await self._load_prefs()
        provider, ref = await self._locate_task(tenant_id, task_id, list_id, prefs)
        done = await provider.complete_task(tenant_id, task_id, list_id=ref.collection or None)
        # If the completed task carried a repeat rule, materialize its next instance and retire
        # the rule on this one — the recurrence moves to the live successor (ADR-0082).
        await self._materialize_next(tenant_id, provider, ref, done)
        await self._emit_completed(tenant_id, done, ref)
        return done

    async def update_task(
        self,
        tenant_id: str,
        task_id: str,
        *,
        title: str | None = None,
        notes: str | None = None,
        due: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        tags: list[str] | None = None,
        list_id: str | None = None,
        to_list_id: str | None = None,
        repeat: str | None = None,
    ) -> Task:
        prefs = await self._load_prefs()
        provider, ref = await self._locate_task(tenant_id, task_id, list_id, prefs)
        if to_list_id is not None:
            target_provider, target_ref = self._resolve_collection(to_list_id, prefs)
            if (target_ref.account, target_ref.collection) != (ref.account, ref.collection):
                # Cross-list move: recreate in the target then delete the source (ADR-0038) —
                # Google Tasks has no move-between-lists API, so a move can't be an in-place
                # edit. Field edits supplied alongside the move are applied to the new task.
                return await self._move_task(
                    tenant_id,
                    task_id,
                    source=(provider, ref),
                    target=(target_provider, target_ref),
                    title=title,
                    notes=notes,
                    due=due,
                    status=status,
                    priority=priority,
                    tags=tags,
                    repeat=repeat,
                )
        updated = await provider.update_task(
            tenant_id,
            task_id,
            title=title,
            notes=notes,
            due=due,
            status=status,
            priority=priority,
            tags=tags,
            list_id=ref.collection or None,
            repeat=repeat,
        )
        await self._emit_updated(tenant_id, updated, ref)
        return updated

    async def delete_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> None:
        prefs = await self._load_prefs()
        provider, ref = await self._locate_task(tenant_id, task_id, list_id, prefs)
        await provider.delete_task(tenant_id, task_id, list_id=ref.collection or None)

    async def _move_task(
        self,
        tenant_id: str,
        task_id: str,
        *,
        source: tuple[TasksProvider, CollectionRef],
        target: tuple[TasksProvider, CollectionRef],
        title: str | None,
        notes: str | None,
        due: str | None,
        status: str | None,
        priority: str | None,
        tags: list[str] | None,
        repeat: str | None,
    ) -> Task:
        """Move a task to another list by recreating it in *target* and deleting the source.

        Adds the new task before deleting the old, so a failure mid-move never loses the
        task (worst case a transient duplicate). The created task is stamped with the target
        list so the board shows it under its new category. Edits passed with the move
        overlay the source task's current values. A recurring task keeps its rule across the
        move (the new list owns it, and — for Google — the source's side-table rule is retired
        by ``delete_task``'s GC, ADR-0082); ``repeat=""`` alongside the move clears it.
        """
        source_provider, source_ref = source
        target_provider, target_ref = target
        current = await source_provider.get_task(
            tenant_id, task_id, list_id=source_ref.collection or None
        )
        if current is None:
            raise ValueError(f"task {task_id!r} not found")
        created = await target_provider.add_task(
            tenant_id,
            title if title is not None else current.title,
            notes=notes if notes is not None else current.notes,
            due=due if due is not None else current.due,
            status=status if status is not None else current.status,
            priority=priority if priority is not None else current.priority,
            tags=tags if tags is not None else current.tags,
            list_id=target_ref.collection or None,
            repeat=repeat if repeat is not None else current.repeat,
        )
        await source_provider.delete_task(tenant_id, task_id, list_id=source_ref.collection or None)
        titles = await self._title_map(tenant_id, [target_ref])
        moved = self._stamp(
            [created], ref=target_ref, title=titles.get((target_ref.account, target_ref.collection))
        )[0]
        await self._emit_moved(tenant_id, moved, source_ref=source_ref, target_ref=target_ref)
        return moved

    # ── event spine (#664) ──────────────────────────────────────────────────

    async def _emit_created(self, tenant_id: str, task: Task, ref: CollectionRef) -> None:
        if self._bus is None:
            return
        try:
            await emit_event(
                self._bus,
                tenant_id=tenant_id,
                module="tasks",
                event_type="tasks.task_created",
                dedup_key=f"{ref.account}:{task.id}",
                payload=_task_summary_payload(task),
                entity_ref=EntityRef(ref_id=task.id, module="tasks", kind="task", title=task.title),
            )
        except Exception as exc:  # a spine hiccup must never fail an already-completed write
            log.warning("tasks.task_created emit failed", task_id=task.id, error=str(exc))

    async def _emit_completed(self, tenant_id: str, task: Task, ref: CollectionRef) -> None:
        if self._bus is None:
            return
        try:
            await emit_event(
                self._bus,
                tenant_id=tenant_id,
                module="tasks",
                event_type="tasks.task_completed",
                dedup_key=f"{ref.account}:{task.id}",
                payload=_task_summary_payload(task),
                entity_ref=EntityRef(ref_id=task.id, module="tasks", kind="task", title=task.title),
            )
        except Exception as exc:
            log.warning("tasks.task_completed emit failed", task_id=task.id, error=str(exc))

    async def _emit_updated(self, tenant_id: str, task: Task, ref: CollectionRef) -> None:
        if self._bus is None:
            return
        try:
            await emit_event(
                self._bus,
                tenant_id=tenant_id,
                module="tasks",
                event_type="tasks.task_updated",
                # Same "dedup provider id + change hash" posture as calendar.event_updated
                # (#664): a genuinely different edit gets its own dedup_key so the core's
                # log records it as its own entry rather than merging it away.
                dedup_key=f"{ref.account}:{task.id}:{_task_change_hash(task)}",
                payload=_task_summary_payload(task),
                entity_ref=EntityRef(ref_id=task.id, module="tasks", kind="task", title=task.title),
            )
        except Exception as exc:
            log.warning("tasks.task_updated emit failed", task_id=task.id, error=str(exc))

    async def _emit_moved(
        self, tenant_id: str, task: Task, *, source_ref: CollectionRef, target_ref: CollectionRef
    ) -> None:
        if self._bus is None:
            return
        payload = _task_summary_payload(task)
        payload["from_list"] = source_ref.collection or _LOCAL_TITLE
        payload["to_list"] = target_ref.collection or _LOCAL_TITLE
        try:
            await emit_event(
                self._bus,
                tenant_id=tenant_id,
                module="tasks",
                event_type="tasks.task_moved",
                # The move recreates the task in the target (Google Tasks has no move API,
                # ADR-0038), so the *new* id is the one thing that uniquely names this move.
                dedup_key=f"{target_ref.account}:{task.id}",
                payload=payload,
                entity_ref=EntityRef(ref_id=task.id, module="tasks", kind="task", title=task.title),
            )
        except Exception as exc:
            log.warning("tasks.task_moved emit failed", task_id=task.id, error=str(exc))

    async def _materialize_next(
        self, tenant_id: str, provider: TasksProvider, ref: CollectionRef, done: Task
    ) -> Task | None:
        """Create the next instance of a just-completed recurring task (ADR-0082).

        No-op for a one-off task (no ``repeat``). The actual "compute due, spawn a successor,
        retire the rule" mechanics are shared with the overdue sweep (#515) — see
        :meth:`_materialize`.
        """
        if not done.repeat:
            return None
        # Resolve "today" once for this completion and thread it in, like the read path (#553),
        # so materialization does no timezone fetch inside the claimed window.
        created, _retired = await self._materialize(
            tenant_id, provider, coll=ref.collection or None, anchor=done, today=await self._now()
        )
        return created

    async def _sweep_overdue(
        self,
        tenant_id: str,
        provider: TasksProvider,
        ref: CollectionRef,
        tasks: list[Task],
        *,
        today: str,
    ) -> list[Task]:
        """Materialize a fresh instance for any open, overdue recurring task in *tasks* (#515).

        *today* is resolved once by the caller (:meth:`list_tasks`) and threaded in, so a
        multi-collection read makes one operator-timezone fetch rather than one per collection,
        and the sweep and :meth:`_materialize` key off the *same* day (#553).

        On-complete materialization (:meth:`_materialize_next`) is the only trigger otherwise,
        so a recurring task nobody ever completes just sits overdue forever. This runs lazily
        on every read instead of a periodic job — simpler (no scheduler/lifespan wiring to
        add/shut down), and reads (the board, ``tasks_list``) are frequent enough that staleness
        is bounded to "until the next read". Naturally idempotent: materializing retires the
        swept task's rule via :meth:`_materialize`, so it is never swept twice.

        Judgment call (flagged, easy to change): the overdue task itself is left exactly as it
        is — still open, still overdue — only its *recurrence* moves on to a fresh successor.
        This mirrors the skip-missed policy already governing a *late completion*: the series
        advances regardless of lateness, but unlike completing, a sweep never marks a task done
        on the operator's behalf — whether to still do the overdue one (or delete it) stays
        their call. Returns *tasks* with any newly materialized successors appended, and any
        swept task's ``repeat`` reflected as cleared, so this same read is accurate rather than
        waiting for the next one to catch up.
        """
        coll = ref.collection or None
        out: list[Task] = []
        fresh: list[Task] = []
        for task in tasks:
            if task.completed or not task.repeat or not task.due or task.due[:10] >= today:
                out.append(task)
                continue
            created, retired = await self._materialize(
                tenant_id, provider, coll=coll, anchor=task, today=today
            )
            # Only reflect the clear in-memory if the retire write actually landed — if it
            # didn't (logged in `_retire_rule`), this read still truthfully shows the live rule.
            out.append(task.model_copy(update={"repeat": None}) if retired else task)
            if created is not None:
                fresh.append(created)
        return [*out, *fresh]

    async def _materialize(
        self, tenant_id: str, provider: TasksProvider, *, coll: str | None, anchor: Task, today: str
    ) -> tuple[Task | None, bool]:
        """Spawn *anchor*'s next occurrence and retire its rule — shared core (#515).

        Used by both on-complete materialization and the overdue sweep: either way, once
        *anchor* has fired (completed or swept), its rule must move to the new successor so
        *anchor* itself is never materialized again — that is what makes both callers
        double-fire-safe. *today* (resolved once by the caller, #553) anchors the next-due
        computation; if the series is exhausted (a ``COUNT``/``UNTIL`` rule yields no later
        occurrence) or the computation itself fails, nothing is created — a genuine parse issue
        leaves the rule in place so it can be retried rather than silently dropping the
        recurrence, and a materialization failure must never break the caller (a completion or a
        read). Returns ``(created, retired)``: the new instance (or ``None``), and whether the
        source's rule was actually cleared.

        Cancellation-safe (#553): the claim is taken synchronously before the ``try`` and every
        ``await`` between claim and release runs under an ``except BaseException`` that releases
        the claim and re-raises. A plain ``except Exception`` would sail past ``CancelledError``
        (it is a ``BaseException``, not an ``Exception``, since 3.8), leaking the claim forever —
        the anchor's recurrence would then never materialize again until a process restart.
        """
        if not anchor.repeat:
            # Both callers pre-filter to repeating tasks (_materialize_next's early return,
            # the sweep's skip) — this guard keeps that invariant, and the `str` narrowing
            # for `next_due`, inside the one function that relies on it.
            return None, False
        key = (tenant_id, anchor.id)
        if not self._claim_materialize(key):
            # Someone else already has this anchor — a concurrent call (#533a) or a
            # previously stuck one (#533b). Either way, treat this pass as a no-op: the
            # caller sees the anchor unchanged, and the next read picks up wherever the
            # in-flight (or stuck) attempt leaves things.
            return None, False
        # Whether add_task has already landed a successor. It flips the cancellation cleanup
        # from "release the claim" to "keep it": a live rule on the anchor plus an existing
        # successor is exactly the #533b state that re-materializes a duplicate on the next
        # read, so a cancel after the add must keep the claim, not free it for a retry.
        created_ok = False
        try:
            try:
                upcoming = next_due(anchor.due, anchor.repeat, today=today)
            except Exception as exc:
                log.warning(
                    "recurring task: could not compute next due; not materializing",
                    task_id=anchor.id,
                    error=str(exc),
                )
                self._release_materialize(key, created=False, retired=False)
                return None, False
            if upcoming is None:
                # Series exhausted (COUNT/UNTIL) — still retire so the spent rule isn't
                # re-evaluated (or re-swept) again.
                retired = await self._retire_rule(tenant_id, provider, anchor.id, coll=coll)
                self._release_materialize(key, created=False, retired=retired)
                return None, retired
            try:
                created = await provider.add_task(
                    tenant_id,
                    anchor.title,
                    notes=anchor.notes,
                    due=upcoming,
                    priority=anchor.priority,
                    tags=anchor.tags,
                    repeat=anchor.repeat,
                    list_id=coll,
                )
            except Exception as exc:
                # Same principle as the next_due failure above: leave the rule in place so this
                # is retried on the next completion/sweep rather than silently losing it (#515).
                log.warning(
                    "recurring task: could not create the next instance; not retiring the rule",
                    task_id=anchor.id,
                    error=str(exc),
                )
                self._release_materialize(key, created=False, retired=False)
                return None, False
            created_ok = True
            # Retire the rule on the source (add-before-retire, like a list move, so a failure
            # here never loses the recurrence outright — worst case is a residual live rule, not
            # a silently dropped one). "" clears it on both providers.
            retired = await self._retire_rule(tenant_id, provider, anchor.id, coll=coll)
            self._release_materialize(key, created=True, retired=retired)
            return created, retired
        except BaseException:
            # A cancellation (client disconnect, core timeout) — or any other BaseException the
            # inner `except Exception` handlers don't catch — struck between the claim and a
            # normal release. Resolve the claim synchronously (never `await` on a cancellation
            # path: the coroutine re-raises CancelledError on the next await, which would skip
            # cleanup) and re-raise. Pass created_ok as `created`: not-yet-created releases for a
            # retry; already-created keeps the claim, the #533b terminal state, so the still-live
            # rule can't re-materialize a duplicate on the next read.
            self._release_materialize(key, created=created_ok, retired=False)
            raise

    def _claim_materialize(self, key: tuple[str, str]) -> bool:
        """Try to claim *key* (``(tenant_id, task_id)``) for materialization (#533).

        Closes two holes in the on-read sweep flagged in the #517/#528 review: (a) two
        simultaneous ``list_tasks`` calls double-materializing the same overdue anchor (no
        lock/transaction), and (b) a persistently failing retire spawning a fresh duplicate
        on every subsequent read instead of failing once.

        Both callers of :meth:`_materialize` (the on-complete path and the overdue sweep)
        pre-filter to repeating tasks, so by the time this is called the only question is:
        is *key* already spoken for? Checking membership and adding to
        ``self._materializing`` happen with no ``await`` in between, so — in this
        single-process, cooperative-scheduling reality — the check-then-claim is atomic
        against any other concurrent call reaching the same key; whichever call gets here
        first wins the race, and every later one until release() sees it taken.

        Args:
            key: ``(tenant_id, task_id)`` identifying the anchor being materialized.

        Returns:
            ``True`` if the caller may proceed (the key is now claimed); ``False`` if it's
            already claimed and the caller should treat this pass as a no-op.
        """
        if key in self._materializing:
            return False
        self._materializing.add(key)
        return True

    def _release_materialize(self, key: tuple[str, str], *, created: bool, retired: bool) -> None:
        """Resolve a claim taken by :meth:`_claim_materialize` (#533).

        A claim is released — so a later sweep/completion may retry this anchor — in every
        case except one: a successor was *created* but the retire that should have cleared
        the source's rule did not land. That case is kept claimed forever, which is what
        stops the unbounded-duplicate amplification (#533b): the existing ``_retire_rule``
        docstring already treats "retire failed twice" as terminal — "the operator can
        always clear a stray rule by hand" — so no automatic recovery path is actually lost
        by refusing to re-materialize an anchor stuck in that state.

        Args:
            key: the same ``(tenant_id, task_id)`` pair passed to `_claim_materialize`.
            created: whether a successor was created this attempt.
            retired: whether the source's rule was actually cleared this attempt.
        """
        if created and not retired:
            return
        self._materializing.discard(key)

    async def _retire_rule(
        self, tenant_id: str, provider: TasksProvider, task_id: str, *, coll: str | None
    ) -> bool:
        """Clear a materialized task's repeat rule, retrying once before giving up (#515).

        This write lands *after* the successor already exists — the dangerous failure mode:
        if it doesn't land, the rule survives on *task_id* and a later re-completion or
        overdue sweep would spawn a duplicate successor (the exact double-fire this write
        exists to prevent). One retry absorbs a transient blip; if it still fails, this logs
        at error level and gives up rather than raising or looping further — a materialization
        side-effect must never fail the caller's completion or read, and the operator can
        always clear a stray rule by hand (an idempotent ``tasks_update(repeat="")``). Returns
        whether the write ultimately landed, so a caller can keep its in-memory view honest.
        """
        try:
            await provider.update_task(tenant_id, task_id, repeat="", list_id=coll)
            return True
        except Exception as exc:
            log.warning(
                "recurring task: retire write failed; retrying once",
                task_id=task_id,
                error=str(exc),
            )
        try:
            await provider.update_task(tenant_id, task_id, repeat="", list_id=coll)
            return True
        except Exception as exc:
            log.error(
                "recurring task: could not retire the repeat rule after a retry — a"
                " duplicate successor is possible on the next completion/sweep",
                task_id=task_id,
                error=str(exc),
            )
            return False

    async def get_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> Task | None:
        prefs = await self._load_prefs()
        if list_id is not None:
            target, ref = self._resolve_collection(list_id, prefs)
            return await target.get_task(tenant_id, task_id, list_id=ref.collection or None)
        # No list given (resolver / attachment): search active → enabled → local.
        for ref in self._search_refs(prefs):
            provider = self._provider_for(ref.account)
            if provider is None:
                continue
            try:
                task = await provider.get_task(tenant_id, task_id, list_id=ref.collection or None)
            except Exception as exc:
                log.warning(
                    "task lookup failed; trying next source (#209)",
                    account=ref.account,
                    collection=ref.collection,
                    error=str(exc),
                )
                continue
            if task is not None:
                return task
        return None

    async def is_available(self, tenant_id: str) -> bool:
        # The local default is always available, so tasks is never "unavailable".
        return True

    async def list_collections(self, tenant_id: str) -> list[Collection]:
        # Discovery is driven from the external providers directly (see /accounts).
        return []

    async def create_list(self, tenant_id: str, title: str) -> Collection:
        """Create a new list under the operator's connected external account (#474).

        Routes to the sole *configured* external provider (Google, today — the local
        store has no create_list of its own, ADR-0030). Raises a clear, actionable error
        if none is configured, or if more than one is (``tasks_create_list`` takes no
        provider selector, so a second external provider type would be ambiguous — not
        reachable today since only Google is implemented, but the router already holds
        an arbitrary map of providers). A provider that is configured but not actually
        *connected* raises its own "not connected" error (e.g. Google's `_access_token`) —
        no need to duplicate that check here.
        """
        if not self._external:
            raise ValueError("no external account connected — connect Google to create a list")
        if len(self._external) > 1:
            raise ValueError(
                "more than one external provider is configured — tasks_create_list can't"
                " yet tell them apart"
            )
        provider = next(iter(self._external.values()))
        return await provider.create_list(tenant_id, title)

    # ── routing & tagging helpers ──────────────────────────────────────────────

    async def _locate_task(
        self, tenant_id: str, task_id: str, list_id: str | None, prefs: CollectionPrefs
    ) -> tuple[TasksProvider, CollectionRef]:
        """The provider + ref owning *task_id*, for a mutation on an *existing* task.

        An explicit *list_id* is honored as-is (delegates to :meth:`_resolve_collection`,
        no search needed). With none, this searches active → other enabled → local — the
        same order as :meth:`get_task` — so ``complete_task`` / ``update_task`` /
        ``delete_task`` reach the list the task actually lives in instead of assuming the
        default write target (#475): a task added to a non-default list would otherwise have
        its mutation routed to the active/first-enabled list and 404 there. A source that
        errors is skipped, not fatal (#209). If the task isn't found anywhere, falls back to
        the default write target so a genuinely bad id still gets the provider's own
        not-found error, unchanged from before this search existed.
        """
        if list_id is not None:
            return self._resolve_collection(list_id, prefs)
        for ref in self._search_refs(prefs):
            provider = self._provider_for(ref.account)
            if provider is None:
                continue
            try:
                task = await provider.get_task(tenant_id, task_id, list_id=ref.collection or None)
            except Exception as exc:
                log.warning(
                    "task lookup failed while locating a mutation target;"
                    " trying next source (#209)",
                    account=ref.account,
                    collection=ref.collection,
                    error=str(exc),
                )
                continue
            if task is not None:
                return provider, ref
        return self._resolve_collection(list_id, prefs)

    def _resolve_collection(
        self, list_id: str | None, prefs: CollectionPrefs
    ) -> tuple[TasksProvider, CollectionRef]:
        """The provider + ref a write/mutation targets.

        ``list_id`` given → the enabled ref whose collection matches (else the sole external
        account, else local); ``None`` → the default write target: the active list, else the
        first enabled, else local.
        """
        if list_id is None:
            ref = prefs.active or (prefs.enabled[0] if prefs.enabled else _LOCAL_REF)
            return self._provider_for(ref.account) or self._local, ref
        for ref in prefs.enabled:
            provider = self._provider_for(ref.account)
            if ref.collection == list_id and provider is not None:
                return provider, ref
        if len(self._external) == 1:  # the sole external account owns an unlisted id
            account, provider = next(iter(self._external.items()))
            return provider, CollectionRef(account=account, collection=list_id)
        return self._local, _LOCAL_REF

    async def _title_map(
        self, tenant_id: str, refs: list[CollectionRef]
    ) -> dict[tuple[str, str], str]:
        """``(account, collection) -> title`` for the refs' external accounts.

        One ``list_collections`` call per distinct external account; a failure falls back to
        using the collection id as the label (a title lookup must never fail the board).
        """
        titles: dict[tuple[str, str], str] = {}
        accounts = {ref.account for ref in refs if ref.account != LOCAL_ACCOUNT}
        for account in accounts:
            provider = self._external.get(account)
            if provider is None:
                continue
            try:
                for col in await provider.list_collections(tenant_id):
                    titles[(col.account, col.collection)] = col.title
            except Exception as exc:
                log.warning(
                    "task-list title lookup failed; using ids", account=account, error=str(exc)
                )
        return titles

    @staticmethod
    def _stamp(tasks: list[Task], *, ref: CollectionRef, title: str | None) -> list[Task]:
        """Tag each task with the list (category) it came from (ADR-0036)."""
        if ref.account == LOCAL_ACCOUNT:
            list_id: str | None = None
            label: str | None = _LOCAL_TITLE
        else:
            list_id = ref.collection or None
            label = title or ref.collection or None
        return [t.model_copy(update={"list_id": list_id, "list_title": label}) for t in tasks]

    def _search_refs(self, prefs: CollectionPrefs) -> list[CollectionRef]:
        """Ordered, de-duplicated places to find one task by id (active → enabled → local)."""
        refs: list[CollectionRef] = []
        if prefs.active is not None:
            refs.append(prefs.active)
        refs.extend(prefs.enabled)
        refs.append(_LOCAL_REF)
        ordered: list[CollectionRef] = []
        seen: set[tuple[str, str]] = set()
        for ref in refs:
            key = (ref.account, ref.collection)
            if key in seen:
                continue
            seen.add(key)
            ordered.append(ref)
        return ordered

    async def _load_prefs(self) -> CollectionPrefs:
        """The operator's selection, falling back to local-only if the core is unreachable.

        A prefs read must never break a task read: if the core is down or errors, the
        module quietly falls back to its silent local default (local-first).
        """
        try:
            return await self._prefs.get_collections()
        except Exception as exc:
            log.warning("collection prefs unavailable; using local default", error=str(exc))
            return CollectionPrefs()


def _task_summary_payload(task: Task) -> dict[str, Any]:
    """Pointers + minimal metadata for a task event — never the free-form ``notes`` body
    (#664, mirrors mail's and calendar's payload discipline)."""
    return {
        "title": task.title[:200],
        "due": task.due,
        "status": task.status,
    }


def _task_change_hash(task: Task) -> str:
    """A short, stable fingerprint of a task's mutable fields (#664's "dedup + change hash"
    posture, mirroring calendar.event_updated). Deliberately not Python's ``hash()`` — it is
    salted per-process (``PYTHONHASHSEED``), so identical content would hash differently
    across a restart, breaking the log's dedup guarantee for an update that straddles one.
    """
    fingerprint = {
        "title": task.title,
        "notes": task.notes,
        "due": task.due,
        "status": task.status,
        "priority": task.priority,
        "tags": sorted(task.tags),
    }
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
    return digest[:12]
