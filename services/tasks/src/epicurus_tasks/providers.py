"""TasksProvider Protocol — the swappable-backend seam (ADR-0016)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from epicurus_tasks.models import Task


@runtime_checkable
class TasksProvider(Protocol):
    """Abstract task-store back-end.

    Implementations: :class:`GoogleTasksProvider`, :class:`LocalTasksProvider`.
    Adding a new provider (Todoist, Microsoft To Do, …) requires only a new
    class implementing this Protocol — the MCP tool surface is unchanged.
    """

    def provider_name(self) -> str:
        """Return the short identifier of this provider, e.g. ``"google"``."""
        ...

    async def list_tasks(self, tenant_id: str, *, list_id: str | None = None) -> list[Task]:
        """Return tasks for *tenant_id*.

        Args:
            tenant_id: Tenant scope.
            list_id: Provider-specific list identifier. ``None`` means the
                provider's default list (``@default`` for Google Tasks).
        """
        ...

    async def add_task(
        self,
        tenant_id: str,
        title: str,
        *,
        notes: str | None = None,
        due: str | None = None,
        list_id: str | None = None,
    ) -> Task:
        """Create and return a new task.

        Args:
            tenant_id: Tenant scope.
            title: Task title (required).
            notes: Optional free-text notes.
            due: Optional ISO date string, e.g. ``"2025-01-15"``.
            list_id: Target list; ``None`` means the default list.
        """
        ...

    async def complete_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> Task:
        """Mark a task complete and return the updated task.

        Args:
            tenant_id: Tenant scope.
            task_id: Provider-specific task identifier.
            list_id: List containing the task; ``None`` means the default list.
        """
        ...

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
        """Edit a task's content and return the updated task.

        Only the fields passed (non-``None``) are changed; omitted fields keep
        their current value. Distinct from :meth:`complete_task`, which flips the
        done flag — this edits title/notes/due.

        Args:
            tenant_id: Tenant scope.
            task_id: Provider-specific task identifier.
            title: New title; ``None`` leaves it unchanged.
            notes: New notes; ``None`` leaves them unchanged.
            due: New ISO date string, e.g. ``"2025-01-15"``; ``None`` leaves it unchanged.
            list_id: List containing the task; ``None`` means the default list.
        """
        ...
