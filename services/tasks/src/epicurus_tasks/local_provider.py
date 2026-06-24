"""LocalTasksProvider — tasks stored in the module's own Postgres schema.

Works with no external account.  Proves the provider seam alongside
:class:`GoogleTasksProvider` (ADR-0016 v0.1 requirement).
"""

from __future__ import annotations

from epicurus_core import Collection
from epicurus_tasks.db import TaskStore
from epicurus_tasks.models import Task, TaskScope


class LocalTasksProvider:
    """Stores tasks in the module's tenant-scoped Postgres table.

    ``list_id`` is ignored — the local store has a single, flat list per tenant.
    All richer fields (status, priority, tags) are fully persisted. It is the silent
    default (ADR-0030): always available, and never a selectable account.
    """

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    def provider_name(self) -> str:
        return "local"

    async def list_tasks(
        self, tenant_id: str, *, list_id: str | None = None, scope: TaskScope = "open"
    ) -> list[Task]:
        return await self._store.list_tasks(tenant_id=tenant_id, scope=scope)

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
        return await self._store.add_task(
            tenant_id=tenant_id,
            title=title,
            notes=notes,
            due=due,
            status=status,
            priority=priority,
            tags=tags,
        )

    async def complete_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> Task:
        try:
            return await self._store.complete_task(tenant_id=tenant_id, task_id=task_id)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc

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
        to_list_id: str | None = None,  # ignored: the single local list has nowhere to move to
    ) -> Task:
        try:
            return await self._store.update_task(
                tenant_id=tenant_id,
                task_id=task_id,
                title=title,
                notes=notes,
                due=due,
                status=status,
                priority=priority,
                tags=tags,
            )
        except KeyError as exc:
            raise ValueError(str(exc)) from exc

    async def get_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> Task | None:
        return await self._store.get_task(tenant_id=tenant_id, task_id=task_id)

    async def delete_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> None:
        # Hard-delete; a missing id is a no-op (the store deletes by id without error).
        await self._store.delete_task(tenant_id=tenant_id, task_id=task_id)

    async def is_available(self, tenant_id: str) -> bool:
        return True

    async def list_collections(self, tenant_id: str) -> list[Collection]:
        # Local is the silent default, not a selectable account (ADR-0030).
        return []
