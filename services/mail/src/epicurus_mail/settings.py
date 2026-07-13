"""Mail-service configuration — CoreSettings plus the platform URL."""

from __future__ import annotations

from epicurus_core import CoreSettings


class MailSettings(CoreSettings):
    """Adds the platform API endpoint + local-cache DSN to the shared settings."""

    # Core service base URL (platform API). On the Docker network: http://core-app:8080.
    platform_url: str = "http://localhost:8080"
    # Postgres DSN for the tenant-scoped local mail cache (ADR-0096, #623). On the Docker
    # network: the shared ``postgres`` service. The module owns its own tables; no shared DB.
    database_url: str = "postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus"
