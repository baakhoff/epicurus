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
    # Minimum seconds between mail.sync_failed emissions (#663) — every mailbox page open can
    # trigger a reconcile, so an account stuck failing must not storm the event spine once per
    # open. 15 minutes is frequent enough to notice, sparse enough not to be noise.
    mail_sync_failed_cooldown_s: float = 900.0
    # How long the Inbox's category tabs (#765) stay fresh in the local cache. Assembling them
    # is a provider fan-out (per category: populated?, newest message, unread count), so they
    # must not be rebuilt per render; a minute is short enough that newly arrived mail moves
    # the badges on its own, and our own mark-read drops the cache outright so a count never
    # sits stale after the operator changed it.
    mail_category_ttl_s: float = 60.0
