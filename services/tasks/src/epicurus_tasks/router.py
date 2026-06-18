"""Collection router — routes task ops to the operator's active list (ADR-0030).

Tasks is a single-active (non-``multi``) module: the board, the tools, and the
attachment/resolver surfaces all operate on the **active** collection — the local store
by default, or a connected Google task list once the operator selects one. The router
holds the always-present local store plus each external provider and resolves the active
account + list from the core via a :class:`CollectionPrefsSource` (the module's
``PlatformClient``), falling back to local when nothing is selected or the core is
unreachable.

It satisfies the :class:`~epicurus_tasks.providers.TasksProvider` Protocol, so the
module's tools and board treat it like any other backend. An explicit ``list_id`` still
overrides the active list within the active account.
"""

from __future__ import annotations

from typing import Protocol

from epicurus_core import LOCAL_ACCOUNT, Collection, CollectionPrefs, CollectionRef, get_logger
from epicurus_tasks.models import Task
from epicurus_tasks.providers import TasksProvider

log = get_logger("epicurus_tasks.router")

_LOCAL_REF = CollectionRef(account=LOCAL_ACCOUNT)


class CollectionPrefsSource(Protocol):
    """Returns the operator's stored collection selection (the module's PlatformClient)."""

    async def get_collections(self) -> CollectionPrefs: ...


class TasksRouter:
    """Routes task ops to the active provider + list per the operator's selection."""

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

    async def list_tasks(self, tenant_id: str, *, list_id: str | None = None) -> list[Task]:
        provider, default_list = await self._route()
        return await provider.list_tasks(tenant_id, list_id=list_id or default_list)

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
        provider, default_list = await self._route()
        return await provider.add_task(
            tenant_id,
            title,
            notes=notes,
            due=due,
            status=status,
            priority=priority,
            tags=tags,
            list_id=list_id or default_list,
        )

    async def complete_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> Task:
        provider, default_list = await self._route()
        return await provider.complete_task(tenant_id, task_id, list_id=list_id or default_list)

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
        provider, default_list = await self._route()
        return await provider.update_task(
            tenant_id,
            task_id,
            title=title,
            notes=notes,
            due=due,
            status=status,
            priority=priority,
            tags=tags,
            list_id=list_id or default_list,
        )

    async def get_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> Task | None:
        provider, default_list = await self._route()
        return await provider.get_task(tenant_id, task_id, list_id=list_id or default_list)

    async def is_available(self, tenant_id: str) -> bool:
        # The local default is always available, so tasks is never "unavailable".
        return True

    async def list_collections(self, tenant_id: str) -> list[Collection]:
        # Discovery is driven from the external providers directly (see /accounts).
        return []

    async def _route(self) -> tuple[TasksProvider, str | None]:
        """The provider + default list id for the active collection (local when unset)."""
        ref = (await self._load_prefs()).active or _LOCAL_REF
        if ref.account == LOCAL_ACCOUNT:
            return self._local, None
        provider = self._external.get(ref.account)
        if provider is None:  # selected account no longer configured → degrade to local
            return self._local, None
        return provider, (ref.collection or None)

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
