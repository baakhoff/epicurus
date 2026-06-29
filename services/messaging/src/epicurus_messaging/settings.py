"""Settings for the messaging module — the shared core settings plus bridge selection."""

from __future__ import annotations

from epicurus_core import CoreSettings


class MessagingSettings(CoreSettings):
    """Adds the active-bridge selection to the shared settings."""

    # The active bridge provider. ``loopback`` (default) is the built-in in-process echo
    # bridge — no external service, no secret — so a fresh install has a working path. Real
    # bridges (``telegram``, ``discord``, …) fan out after the foundation (#365+) and read
    # their per-tenant bot token from OpenBao.
    messaging_provider: str = "loopback"
