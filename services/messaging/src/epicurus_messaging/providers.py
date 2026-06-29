"""The bridge-provider seam (ADR-0058, provider-pluggable per ADR-0016).

A :class:`BridgeProvider` is one chat-bridge backend: it receives messages from an external
service (Telegram, Discord, …) and delivers replies to it. The module owns the NATS contract
and drives the provider: it calls :meth:`~BridgeProvider.start` with a handler that publishes
``messaging.inbound``, and subscribes ``messaging.outbound`` to call :meth:`~BridgeProvider.send`.

A provider **never** touches NATS or an LLM (constraint #8) — it only speaks its service's API,
and fetches its per-tenant bot token from the core's OpenBao via :func:`bridge_token`. The
built-in :class:`~epicurus_messaging.loopback_provider.LoopbackProvider` needs no token; the
real bridges fan out after this foundation (Telegram #365, Discord #366, …).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from epicurus_core import InboundMessage, OutboundMessage, SecretError, SecretStore, get_logger

log = get_logger("messaging.providers")

# A provider calls this with each message it receives; the module publishes ``messaging.inbound``.
InboundHandler = Callable[[InboundMessage], Awaitable[None]]


@runtime_checkable
class BridgeProvider(Protocol):
    """A pluggable chat-bridge backend. Adding a bridge = a new class implementing this."""

    def provider_name(self) -> str:
        """The bridge id stamped on every message, e.g. ``"telegram"`` / ``"loopback"``."""
        ...

    def secret_names(self) -> list[str]:
        """OpenBao secret paths this provider needs — surfaced in the manifest ``secrets[]``.

        Empty for a provider that needs no credential (loopback). A real bridge returns e.g.
        ``["messaging/telegram"]`` and reads the token with :func:`bridge_token`.
        """
        ...

    async def start(self, on_inbound: InboundHandler) -> None:
        """Begin receiving from the external service; call ``on_inbound`` per message."""
        ...

    async def send(self, message: OutboundMessage) -> None:
        """Deliver a reply to the channel named by ``message``."""
        ...

    async def stop(self) -> None:
        """Stop receiving and release resources (called on shutdown)."""
        ...


async def bridge_token(secrets: SecretStore, bridge: str, *, tenant: str) -> str | None:
    """Fetch a bridge's per-tenant bot token from OpenBao (``messaging/<bridge>`` → ``token``).

    The one helper every bridge uses, so providers never hand-roll a secret path (the seam the
    foundation ships so the bridges don't diverge — see the OAuth-token lesson in AGENTS.md).
    Returns ``None`` when no token is stored (the bridge is simply "not connected"), never raises
    for the absent case.
    """
    try:
        data = await secrets.get(f"messaging/{bridge}", tenant_id=tenant)
    except SecretError:
        return None
    token = data.get("token")
    return token if isinstance(token, str) and token else None
