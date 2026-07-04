"""TasksProvider Protocol — the swappable-backend seam (ADR-0016)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from epicurus_core import Collection
from epicurus_tasks.models import Task, TaskScope


@runtime_checkable
class TasksProvider(Protocol):
    """Abstract task-store back-end.

    Implementations: :class:`GoogleTasksProvider`, :class:`LocalTasksProvider`.
    Adding a new provider (Todoist, Microsoft To Do, …) requires only a new
    class implementing this Protocol — the MCP tool surface is unchanged.

    Provider field support:
    - title, notes, due, status ("open"/"done"): all providers.
    - status "in_progress": local-only; Google degrades it to "open" on read-back.
    - priority, tags: local-only; Google silently ignores them.
    """

    def provider_name(self) -> str:
        """Return the short identifier of this provider, e.g. ``"google"``."""
        ...

    async def list_tasks(
        self, tenant_id: str, *, list_id: str | None = None, scope: TaskScope = "open"
    ) -> list[Task]:
        """Return tasks for *tenant_id*.

        Args:
            tenant_id: Tenant scope.
            list_id: Provider-specific list identifier. ``None`` means the
                provider's default list (``@default`` for Google Tasks).
            scope: Which tasks to include (ADR-0049) — ``"open"`` (default, excludes
                completed), ``"done"`` (completed only), or ``"all"``. Backs the board's
                *Show* filter so the operator can review completed work.
        """
        ...

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
        """Create and return a new task.

        Args:
            tenant_id: Tenant scope.
            title: Task title (required).
            notes: Optional free-text notes.
            due: Optional ISO date string, e.g. ``"2025-01-15"``.
            status: Initial status (``"open"``/``"in_progress"``/``"done"``).
            priority: Optional priority (``"low"``/``"medium"``/``"high"``).
                Google Tasks silently ignores this field.
            tags: Optional list of string labels. Google Tasks silently ignores them.
            list_id: Target list; ``None`` means the default list.
            repeat: Optional bare RRULE making the task recurring (ADR-0082). Emulated
                module-side (both providers persist it — Google in a side table); completing
                the task later materializes the next instance. Requires a ``due`` to anchor.
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

    async def delete_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> None:
        """Delete a task. A missing task is treated as already-gone (no error).

        Backs moving a task between lists (ADR-0038): Google Tasks has no cross-list
        move, so the router recreates the task in the target list and deletes the
        original here.

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
        status: str | None = None,
        priority: str | None = None,
        tags: list[str] | None = None,
        list_id: str | None = None,
        to_list_id: str | None = None,
        repeat: str | None = None,
    ) -> Task:
        """Edit a task's content and return the updated task.

        Only the fields passed (non-``None``) are changed; omitted fields keep
        their current value. **An empty string clears** ``due``, ``notes``, or
        ``repeat`` (``""`` means "unset", distinct from ``None`` which means "leave
        unchanged") — #475; ``repeat`` #471. Distinct from :meth:`complete_task`, which
        flips the done flag — this edits content. Google Tasks silently ignores priority/tags;
        ``"in_progress"`` status is mapped to ``"open"`` by Google on read-back.

        Args:
            tenant_id: Tenant scope.
            task_id: Provider-specific task identifier.
            title: New title; ``None`` leaves it unchanged.
            notes: New notes; ``None`` leaves them unchanged, ``""`` clears them.
            due: New ISO date string; ``None`` leaves it unchanged, ``""`` clears it.
            status: New status (``"open"``/``"in_progress"``/``"done"``); ``None``
                leaves it unchanged.
            priority: New priority; ``None`` leaves it unchanged.
            tags: New tags list; ``None`` leaves it unchanged.
            list_id: List containing the task; ``None`` means the default list.
            repeat: New bare RRULE; ``None`` leaves it unchanged, ``""`` clears it (turns the
                task one-off). Emulated module-side — ADR-0082.
            to_list_id: Move target — honored by the :class:`~epicurus_tasks.router.TasksRouter`
                (which can move across lists by recreate+delete, ADR-0038). A single provider
                has no cross-list move and **ignores** it (like Google ignores priority/tags).
        """
        ...

    async def get_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> Task | None:
        """Return a single task by id, or ``None`` if it does not exist.

        Backs the chat-attachment source and the entity-ref resolver (ADR-0019):
        the module fetches one attached / referenced task without re-listing.

        Args:
            tenant_id: Tenant scope.
            task_id: Provider-specific task identifier.
            list_id: List containing the task; ``None`` means the default list.
        """
        ...

    async def is_available(self, tenant_id: str) -> bool:
        """True when the provider is configured and reachable for *tenant_id* (ADR-0030).

        The local store is always available; an external provider reports its live
        connection state (e.g. whether Google is connected).
        """
        ...

    async def list_collections(self, tenant_id: str) -> list[Collection]:
        """The collections (task lists) this provider exposes for *tenant_id* (ADR-0030).

        Drives the connected-accounts picker. The local store returns an empty list — it
        is the silent default, never a selectable account.
        """
        ...

    async def create_list(self, tenant_id: str, title: str) -> Collection:
        """Create a new list and return it as a :class:`Collection` (#474).

        Args:
            tenant_id: Tenant scope.
            title: The new list's display name.

        Raises:
            NotImplementedError: A provider with no concept of multiple lists (the local
                store — a single implicit list, never a selectable account, ADR-0030) has
                no list to create; connect an external account instead.
        """
        ...
