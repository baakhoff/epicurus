"""Tasks-service configuration — CoreSettings plus tasks-specific fields."""

from __future__ import annotations

from epicurus_core import CoreSettings


class TasksSettings(CoreSettings):
    """Extends shared settings with tasks-specific configuration.

    There is no provider selection any more (ADR-0030): the module always backs itself
    with the local store and routes to a connected Google task list per the operator's
    selection, which lives in the core (``module_prefs``), not in service config.
    """

    # Core service base URL (platform API).  On the Docker network: http://core-app:8080.
    platform_url: str = "http://localhost:8080"
    # Postgres DSN for the local default store.
    database_url: str = "postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus"

    # How often the lead-time scheduler ticks (#664) — task_due_soon/task_overdue. Day-granular
    # leads don't need calendar's minute-level polling; 5 minutes keeps the provider read cheap
    # while still noticing a new day promptly.
    scheduler_poll_interval_s: float = 300.0
