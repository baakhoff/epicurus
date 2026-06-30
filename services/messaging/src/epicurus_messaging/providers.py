"""The bridge-provider seam (ADR-0058 / ADR-0062, provider-pluggable per ADR-0016).

A :class:`BridgeProvider` is one chat-bridge backend: it receives messages from an external
service (Telegram, Discord, …) and delivers replies to it. The
:class:`~epicurus_messaging.manager.BridgeManager` owns the NATS contract and drives every
provider: it calls :meth:`~BridgeProvider.start` with a handler that publishes
``messaging.inbound``, and dispatches each ``messaging.outbound`` reply to the provider whose
:meth:`~BridgeProvider.provider_name` matches the message's ``bridge``.

A provider **never** touches NATS or an LLM (constraint #8) — it only speaks its service's API,
and fetches its per-tenant bot token from the core's OpenBao via :func:`load_bridge_secret`.
The built-in :class:`~epicurus_messaging.loopback_provider.LoopbackProvider` needs no token; the
real bridges (Discord #366, Telegram #365, …) stay dormant until the operator connects them by
storing a token, then connect at runtime when the core triggers a reload (ADR-0062).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from epicurus_core import (
    InboundMessage,
    OutboundMessage,
    SecretError,
    SecretStore,
    get_logger,
)

log = get_logger("messaging.providers")

# A provider calls this with each message it receives; the manager publishes ``messaging.inbound``.
InboundHandler = Callable[[InboundMessage], Awaitable[None]]


class BridgeStatus(BaseModel):
    """One bridge's live state — the shape the module's ``/status`` reports and the operator
    surface renders (ADR-0062). Provider-agnostic so the web shell is identical per bridge.

    ``configured`` = a bot token is stored; ``enabled`` = the operator's on/off; ``connected`` =
    the live link to the external service is up right now. A bridge delivers messages only when
    all three are true. ``manageable`` is false for the in-process loopback bridge (nothing for
    the operator to connect).
    """

    bridge: str  # the provider id, e.g. "discord" / "loopback"
    label: str  # human name for the shell, e.g. "Discord"
    manageable: bool = True  # operator connects/disconnects it (false: loopback)
    configured: bool = False  # a bot token is stored in OpenBao
    enabled: bool = True  # the operator's on/off switch (kept across reconnects)
    connected: bool = False  # the live link to the external service is up
    detail: str = ""  # a short human summary, e.g. "2 servers · 5 channels" / "no bot token"


@runtime_checkable
class BridgeProvider(Protocol):
    """A pluggable chat-bridge backend. Adding a bridge = a new class implementing this."""

    def provider_name(self) -> str:
        """The bridge id stamped on every message, e.g. ``"discord"`` / ``"loopback"``."""
        ...

    def secret_names(self) -> list[str]:
        """OpenBao secret paths this provider needs — surfaced in the manifest ``secrets[]``.

        Empty for a provider that needs no credential (loopback). A real bridge returns e.g.
        ``["messaging/discord"]`` and reads the token with :func:`load_bridge_secret`.
        """
        ...

    def connected(self) -> bool:
        """Whether the bridge is live — has its credential and is receiving.

        Surfaced as ``connected`` on ``GET /status`` so the shell shows a real bridge that is
        missing its token as *not connected*. The in-process loopback bridge is always live.
        """
        ...

    async def start(self, on_inbound: InboundHandler) -> None:
        """Begin receiving from the external service; call ``on_inbound`` per message.

        A real bridge re-reads its stored token here, so calling ``start`` again (after
        :meth:`stop`) is how the manager reconnects a bridge whose token just changed.
        Starting with no token is a no-op — the bridge simply reports ``connected=False``.
        """
        ...

    async def send(self, message: OutboundMessage) -> None:
        """Deliver a reply to the channel named by ``message`` (no-op if not connected)."""
        ...

    async def stop(self) -> None:
        """Stop receiving and release resources (called on shutdown and before a reload)."""
        ...

    async def status(self) -> BridgeStatus:
        """The bridge's current :class:`BridgeStatus` for the ``/status`` surface."""
        ...


async def load_bridge_secret(
    secrets: SecretStore, bridge: str, *, tenant: str
) -> tuple[str | None, bool]:
    """Load a bridge's per-tenant ``(token, enabled)`` from OpenBao (``messaging/<bridge>``).

    The one helper every bridge uses, so providers never hand-roll a secret path (the seam the
    foundation ships so the bridges don't diverge — see the OAuth-token lesson in AGENTS.md).
    The core's bridge-admin writes ``{token, enabled}`` to this path on connect. Returns
    ``(None, True)`` when no secret is stored (the bridge is simply "not connected"); ``enabled``
    defaults to true so a freshly-connected bridge is on. Never raises for the absent case.
    """
    try:
        data = await secrets.get(f"messaging/{bridge}", tenant_id=tenant)
    except SecretError:
        return None, True
    raw_token = data.get("token")
    token = raw_token if isinstance(raw_token, str) and raw_token else None
    raw_enabled = data.get("enabled", True)
    enabled = bool(raw_enabled) if isinstance(raw_enabled, bool | int) else True
    return token, enabled


async def bridge_token(secrets: SecretStore, bridge: str, *, tenant: str) -> str | None:
    """Fetch just a bridge's per-tenant bot token (``messaging/<bridge>`` → ``token``).

    A thin convenience over :func:`load_bridge_secret` for a provider that ignores the
    ``enabled`` flag (e.g. Telegram #365); returns ``None`` when no token is stored.
    """
    token, _enabled = await load_bridge_secret(secrets, bridge, tenant=tenant)
    return token
