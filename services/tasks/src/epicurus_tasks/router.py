"""Collection router — fans task reads/writes across the operator's selection (ADR-0030/0036).

The router holds the always-present local store plus each external provider (Google) and
routes per the operator's stored selection, fetched from the core via a
:class:`CollectionPrefsSource` (the module's ``PlatformClient``). Tasks is a ``multi``
module — **each enabled list is a category**:

* **reads** (``list_tasks`` with no ``list_id``) aggregate every *enabled* list, tagging
  each task with the list it came from (``list_id`` / ``list_title``); a failing source is
  skipped, not fatal (#209). An explicit ``list_id`` reads just that one list.
* **writes** (``add_task``) and **per-task mutations** (``complete_task`` / ``update_task``)
  target the list that owns the given ``list_id``; with none, the default write target is
  the *active* list, else the first enabled, else local.
* ``get_task`` searches the active, then the other enabled, then local — so a referenced
  task resolves wherever it lives.

It satisfies the :class:`~epicurus_tasks.providers.TasksProvider` Protocol, so the module's
tools and board treat it like any other backend. Reads fall back to the local store when
nothing is enabled or the core is unreachable (local-first).
"""

from __future__ import annotations

from typing import Protocol

from epicurus_core import LOCAL_ACCOUNT, Collection, CollectionPrefs, CollectionRef, get_logger
from epicurus_tasks.models import Task
from epicurus_tasks.providers import TasksProvider

log = get_logger("epicurus_tasks.router")

_LOCAL_REF = CollectionRef(account=LOCAL_ACCOUNT)
_LOCAL_TITLE = "Personal"
"""Category label for the silent local default list (ADR-0036)."""


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
    ) -> None:
        self._local = local
        self._external = external
        self._prefs = prefs

    def provider_name(self) -> str:
        return "tasks"

    def _provider_for(self, account: str) -> TasksProvider | None:
        """The provider backing *account*, or ``None`` if it isn't configured/connected."""
        if account == LOCAL_ACCOUNT:
            return self._local
        return self._external.get(account)

    async def list_tasks(self, tenant_id: str, *, list_id: str | None = None) -> list[Task]:
        prefs = await self._load_prefs()
        if list_id is not None:
            target, ref = self._resolve_collection(list_id, prefs)
            tasks = await target.list_tasks(tenant_id, list_id=ref.collection or None)
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
                tasks = await provider.list_tasks(tenant_id, list_id=ref.collection or None)
            except Exception as exc:
                log.warning(
                    "tasks read failed; skipping this source (#209)",
                    account=ref.account,
                    collection=ref.collection,
                    error=str(exc),
                )
                continue
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
        )

    async def complete_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> Task:
        prefs = await self._load_prefs()
        provider, ref = self._resolve_collection(list_id, prefs)
        return await provider.complete_task(tenant_id, task_id, list_id=ref.collection or None)

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
    ) -> Task:
        prefs = await self._load_prefs()
        provider, ref = self._resolve_collection(list_id, prefs)
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
        )

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

    # ── routing & tagging helpers ──────────────────────────────────────────────

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
