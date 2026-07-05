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
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from epicurus_core import LOCAL_ACCOUNT, Collection, CollectionPrefs, CollectionRef, get_logger
from epicurus_tasks.models import Task, TaskScope
from epicurus_tasks.providers import TasksProvider
from epicurus_tasks.recurrence import next_due

log = get_logger("epicurus_tasks.router")

_LOCAL_REF = CollectionRef(account=LOCAL_ACCOUNT)
_LOCAL_TITLE = "Personal"
"""Category label for the silent local default list (ADR-0036)."""


def _utc_today() -> str:
    """Today's date (UTC) as an ISO string — the default clock for materialization."""
    return datetime.now(UTC).date().isoformat()


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
        now: Callable[[], str] = _utc_today,
    ) -> None:
        self._local = local
        self._external = external
        self._prefs = prefs
        # Clock for recurrence materialization (ADR-0082); injectable so tests are
        # deterministic without freezing the wall clock.
        self._now = now

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
        if list_id is not None:
            target, ref = self._resolve_collection(list_id, prefs)
            tasks = await target.list_tasks(tenant_id, list_id=ref.collection or None, scope=scope)
            tasks = await self._sweep_overdue(tenant_id, target, ref, tasks)
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
            tasks = await self._sweep_overdue(tenant_id, provider, ref, tasks)
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
        return await provider.add_task(
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

    async def complete_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> Task:
        prefs = await self._load_prefs()
        provider, ref = await self._locate_task(tenant_id, task_id, list_id, prefs)
        done = await provider.complete_task(tenant_id, task_id, list_id=ref.collection or None)
        # If the completed task carried a repeat rule, materialize its next instance and retire
        # the rule on this one — the recurrence moves to the live successor (ADR-0082).
        await self._materialize_next(tenant_id, provider, ref, done)
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
        return await provider.update_task(
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
        return self._stamp(
            [created], ref=target_ref, title=titles.get((target_ref.account, target_ref.collection))
        )[0]

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
        created, _retired = await self._materialize(
            tenant_id, provider, coll=ref.collection or None, anchor=done
        )
        return created

    async def _sweep_overdue(
        self, tenant_id: str, provider: TasksProvider, ref: CollectionRef, tasks: list[Task]
    ) -> list[Task]:
        """Materialize a fresh instance for any open, overdue recurring task in *tasks* (#515).

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
        today = self._now()
        coll = ref.collection or None
        out: list[Task] = []
        fresh: list[Task] = []
        for task in tasks:
            if task.completed or not task.repeat or not task.due or task.due[:10] >= today:
                out.append(task)
                continue
            created, retired = await self._materialize(tenant_id, provider, coll=coll, anchor=task)
            # Only reflect the clear in-memory if the retire write actually landed — if it
            # didn't (logged in `_retire_rule`), this read still truthfully shows the live rule.
            out.append(task.model_copy(update={"repeat": None}) if retired else task)
            if created is not None:
                fresh.append(created)
        return [*out, *fresh]

    async def _materialize(
        self, tenant_id: str, provider: TasksProvider, *, coll: str | None, anchor: Task
    ) -> tuple[Task | None, bool]:
        """Spawn *anchor*'s next occurrence and retire its rule — shared core (#515).

        Used by both on-complete materialization and the overdue sweep: either way, once
        *anchor* has fired (completed or swept), its rule must move to the new successor so
        *anchor* itself is never materialized again — that is what makes both callers
        double-fire-safe. Computes the next due date from the rule; if the series is exhausted
        (a ``COUNT``/``UNTIL`` rule yields no later occurrence) or the computation itself fails,
        nothing is created — a genuine parse issue leaves the rule in place so it can be
        retried rather than silently dropping the recurrence, and a materialization failure
        must never break the caller (a completion or a read). Returns ``(created, retired)``:
        the new instance (or ``None``), and whether the source's rule was actually cleared.
        """
        if not anchor.repeat:
            # Both callers pre-filter to repeating tasks (_materialize_next's early return,
            # the sweep's skip) — this guard keeps that invariant, and the `str` narrowing
            # for `next_due`, inside the one function that relies on it.
            return None, False
        try:
            upcoming = next_due(anchor.due, anchor.repeat, today=self._now())
        except Exception as exc:
            log.warning(
                "recurring task: could not compute next due; not materializing",
                task_id=anchor.id,
                error=str(exc),
            )
            return None, False
        if upcoming is None:
            # Series exhausted (COUNT/UNTIL) — still retire so the spent rule isn't
            # re-evaluated (or re-swept) again.
            retired = await self._retire_rule(tenant_id, provider, anchor.id, coll=coll)
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
            return None, False
        # Retire the rule on the source (add-before-retire, like a list move, so a failure
        # here never loses the recurrence outright — worst case is a residual live rule, not
        # a silently dropped one). "" clears it on both providers.
        retired = await self._retire_rule(tenant_id, provider, anchor.id, coll=coll)
        return created, retired

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
