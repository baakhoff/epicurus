"""Settings for the messaging module — the shared core settings plus bridge selection."""

from __future__ import annotations

from epicurus_core import CoreSettings


class MessagingSettings(CoreSettings):
    """The shared core settings; the module needs no bridge-specific settings of its own."""

    # Retained for backward compatibility only (ADR-0062): bridge selection is no longer a
    # single env choice. The module now runs every bridge at once — the always-on loopback
    # echo plus each real bridge, which stays dormant until its per-tenant bot token is stored
    # in OpenBao and the operator connects it from the web surface (#369). This value is
    # ignored; ``extra="ignore"`` keeps an existing ``MESSAGING_PROVIDER`` env harmless.
    messaging_provider: str = "loopback"

    # Telegram bridge knobs (used when ``messaging_provider == "telegram"``). The bot token
    # itself is never here — it is read per-tenant from OpenBao (``messaging/telegram``).
    # ``telegram_api_base`` is overridable so tests can point at a local mock.
    telegram_api_base: str = "https://api.telegram.org"
    telegram_poll_timeout: int = 30  # getUpdates long-poll seconds (server holds the request)
