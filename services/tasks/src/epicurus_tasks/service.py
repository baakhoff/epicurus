"""Tasks module — provider-agnostic MCP tool surface (ADR-0016)."""

from __future__ import annotations

from epicurus_core import EpicurusModule, UiAction, UiSection
from epicurus_tasks.google_provider import GoogleTasksError
from epicurus_tasks.models import Task
from epicurus_tasks.providers import TasksProvider

MODULE_NAME = "tasks"


def build_module(provider: TasksProvider, *, tenant_id: str) -> EpicurusModule:
    """Register the three provider-agnostic task tools on the module.

    The tools are closed over *provider* and *tenant_id* at build time so the
    MCP tool signatures stay clean (no plumbing arguments leaked to the agent).
    """
    module = EpicurusModule(
        MODULE_NAME,
        version="0.1.0",
        description=(
            f"Task management via the {provider.provider_name()!r} provider: "
            "list, add, and complete tasks."
        ),
        ui=UiSection(
            icon="check-square",
            summary=(
                "Manage your tasks. The active provider is "
                f"{provider.provider_name()!r}; switch providers via "
                "TASKS_PROVIDER in .env without changing these tools."
            ),
            status_url="/status",
            actions=[
                UiAction(
                    tool="tasks_list",
                    label="List tasks",
                    description="Show all open tasks from the active provider.",
                )
            ],
        ),
    )

    @module.tool()
    async def tasks_list(list_id: str | None = None) -> list[Task]:
        """Return open tasks from the active provider.

        Args:
            list_id: Provider-specific list identifier.  Omit to use the
                provider's default list (e.g. ``"@default"`` for Google Tasks).

        Returns a list of :class:`Task` objects (id, title, notes, due,
        completed, completed_at).
        """
        try:
            return await provider.list_tasks(tenant_id, list_id=list_id)
        except (GoogleTasksError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    @module.tool()
    async def tasks_add(
        title: str,
        notes: str | None = None,
        due: str | None = None,
        list_id: str | None = None,
    ) -> Task:
        """Create a new task.

        Args:
            title: Task title (required).
            notes: Optional free-text notes or description.
            due: Optional due date as an ISO date string, e.g. ``"2025-01-15"``.
            list_id: Target list identifier.  Omit for the default list.

        Returns the created :class:`Task`.
        """
        try:
            return await provider.add_task(tenant_id, title, notes=notes, due=due, list_id=list_id)
        except (GoogleTasksError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    @module.tool()
    async def tasks_complete(task_id: str, list_id: str | None = None) -> Task:
        """Mark a task as complete.

        Args:
            task_id: The provider-specific task identifier (from ``tasks_list``).
            list_id: The list containing the task.  Omit for the default list.

        Returns the updated :class:`Task` with ``completed=True``.
        """
        try:
            return await provider.complete_task(tenant_id, task_id, list_id=list_id)
        except (GoogleTasksError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    return module
