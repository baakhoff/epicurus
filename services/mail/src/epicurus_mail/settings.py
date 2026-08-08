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
    # How often the background poller reconciles the mailbox (#796) — the loop that makes
    # ``mail.received`` fire without the Mail page being open. **On by default**: an event
    # contract that only holds while a human is looking at the page isn't a contract, and every
    # downstream consumer (automations, per-event push alerts) is already wired for it. A tick
    # with no connected account costs nothing (a token-presence check, then return); a tick with
    # one is a single history-delta call. Five minutes keeps that well inside any provider's
    # quota while still being "promptly" for mail. ``0`` disables the loop entirely.
    mail_poll_interval_s: float = 300.0
