"""Tasks-service configuration — CoreSettings plus tasks-specific fields."""

from __future__ import annotations

from epicurus_core import CoreSettings


class TasksSettings(CoreSettings):
    """Extends shared settings with tasks-specific configuration."""

    # Which provider to activate: "local" or "google".
    tasks_provider: str = "local"
    # Core service base URL (platform API).  On the Docker network: http://core-app:8080.
    platform_url: str = "http://localhost:8080"
    # Postgres DSN — only used by the local provider.
    database_url: str = "postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus"
