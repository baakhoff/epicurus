"""Calendar-service configuration — CoreSettings plus calendar-specific fields."""

from __future__ import annotations

from epicurus_core import CoreSettings


class CalendarSettings(CoreSettings):
    """Adds storage endpoints to shared settings.

    There is no provider selection any more (ADR-0030): the module always backs itself
    with the local store and routes to connected Google calendars per the operator's
    selection, which lives in the core (``module_prefs``), not in service config.
    """

    # Async Postgres DSN for the local default store.
    database_url: str = "postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus"

    # Core service base URL (platform API). On the Docker network: http://core-app:8080.
    platform_url: str = "http://localhost:8080"
