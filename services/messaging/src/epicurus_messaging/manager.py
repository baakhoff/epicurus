"""The bridge manager — runs every bridge at once and routes between them and NATS (ADR-0062).

The foundation (#364) ran a single active bridge chosen by a setting. Phase 4 needs several
bridges live together (loopback for dev + the operator's connected Telegram/Discord/…), so the
manager holds them all: it starts each, **dispatches each ``messaging.outbound`` reply to the
provider named by the message's ``bridge``**, and aggregates their status. A bridge with no
token stays dormant (``connected=False``) until the operator connects it, at which point the
core triggers :meth:`reload` to (re)connect just that one — no module restart (ADR-0062).
"""

from __future__ import annotations

from epicurus_core import (
    MESSAGING_INBOUND,
    MESSAGING_OUTBOUND,
    OutboundMessage,
    get_logger,
)
from epicurus_messaging.loopback_provider import LoopbackProvider
from epicurus_messaging.providers import BridgeProvider, BridgeStatus, InboundHandler

log = get_logger("messaging.manager")


class BridgeManager:
    """Owns the set of bridge providers and the inbound/outbound routing between them."""

    def __init__(self, providers: list[BridgeProvider], *, loopback: LoopbackProvider) -> None:
        # Keyed by ``provider_name`` so an outbound reply routes by its ``bridge`` field.
        self._providers: dict[str, BridgeProvider] = {p.provider_name(): p for p in providers}
        self._loopback = loopback
        self._on_inbound: InboundHandler | None = None

    @property
    def loopback(self) -> LoopbackProvider:
        """The always-on in-process echo bridge (drives ``/loopback/inject`` and smoke)."""
        return self._loopback

    def provider_names(self) -> list[str]:
        return list(self._providers)

    def secret_names(self) -> list[str]:
        """The union of every bridge's OpenBao secret paths, for the manifest ``secrets[]``."""
        names: set[str] = set()
        for provider in self._providers.values():
            names.update(provider.secret_names())
        return sorted(names)

    async def start_all(self, on_inbound: InboundHandler) -> None:
        """Start every bridge. Best-effort per bridge: one failing to connect never blocks the
        others (a dormant or mis-tokened bridge simply reports ``connected=False``)."""
        self._on_inbound = on_inbound
        for name, provider in self._providers.items():
            try:
                await provider.start(on_inbound)
            except Exception as exc:  # never let one bridge's startup abort the module
                log.error("bridge failed to start", bridge=name, error=str(exc))

    async def stop_all(self) -> None:
        """Stop every bridge (best-effort), called on shutdown."""
        for name, provider in self._providers.items():
            try:
                await provider.stop()
            except Exception as exc:
                log.warning("bridge failed to stop cleanly", bridge=name, error=str(exc))

    async def dispatch(self, message: OutboundMessage) -> None:
        """Deliver one reply via the bridge it came in on (dropped+logged if unknown)."""
        provider = self._providers.get(message.bridge)
        if provider is None:
            log.warning("no bridge for outbound reply", bridge=message.bridge)
            return
        await provider.send(message)

    async def reload(self, bridge: str) -> BridgeStatus:
        """Reconnect one bridge after its token/enabled changed (stop, then start) (ADR-0062).

        Raises :class:`KeyError` for an unknown bridge and :class:`RuntimeError` if the manager
        was never started (no inbound handler yet). Returns the bridge's fresh status.
        """
        provider = self._providers[bridge]  # KeyError → 404 at the edge
        if self._on_inbound is None:
            raise RuntimeError("bridge manager not started")
        await provider.stop()
        await provider.start(self._on_inbound)
        status = await provider.status()
        log.info(
            "bridge reloaded",
            bridge=bridge,
            configured=status.configured,
            enabled=status.enabled,
            connected=status.connected,
        )
        return status

    async def status(self) -> dict[str, object]:
        """The module's ``/status`` body: the two subjects + each bridge's status."""
        bridges = [await provider.status() for provider in self._providers.values()]
        return {
            "inbound_subject": MESSAGING_INBOUND,
            "outbound_subject": MESSAGING_OUTBOUND,
            "bridges": [b.model_dump() for b in bridges],
        }
