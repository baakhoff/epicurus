"""LocalTasksProvider — tasks stored in the module's own Postgres schema.

Works with no external account.  Proves the provider seam alongside
:class:`GoogleTasksProvider` (ADR-0016 v0.1 requirement).
"""

from __future__ import annotations

from epicurus_tasks.db import TaskStore
from epicurus_tasks.models import Task


class LocalTasksProvider:
    """Stores tasks in the module's tenant-scoped Postgres table.

    ``list_id`` is ignored — the local store has a single, flat list per tenant.
    """

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    def provider_name(self) -> str:
        return "local"

    async def list_tasks(self, tenant_id: str, *, list_id: str | None = None) -> list[Task]:
        return await self._store.list_tasks(tenant_id=tenant_id)

    async def add_task(
        self,
        tenant_id: str,
        title: str,
        *,
        notes: str | None = None,
        due: str | None = None,
        list_id: str | None = None,
    ) -> Task:
        return await self._store.add_task(tenant_id=tenant_id, title=title, notes=notes, due=due)

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
        list_id: str | None = None,
    ) -> Task:
        try:
            return await self._store.update_task(
                tenant_id=tenant_id, task_id=task_id, title=title, notes=notes, due=due
            )
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
