"""Calendar-service configuration — CoreSettings plus calendar-specific fields."""

from __future__ import annotations

from epicurus_core import CoreSettings


class CalendarSettings(CoreSettings):
    """Adds provider selection and storage endpoints to shared settings."""

    # Which backing provider to use: "local" (Postgres, default) or "google".
    # The local provider works with no external account; google requires the
    # tenant to have completed the Google OAuth connect flow via the core.
    calendar_provider: str = "local"

    # Google Calendar ID (used by the Google provider only).
    # "primary" resolves to the authenticated user's default calendar.
    calendar_google_id: str = "primary"

    # Async Postgres DSN for the local provider's event store.
    database_url: str = "postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus"

    # Core service base URL (platform API). On the Docker network: http://core-app:8080.
    platform_url: str = "http://localhost:8080"
