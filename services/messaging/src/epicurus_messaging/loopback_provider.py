"""The loopback bridge — an in-process echo provider for development + smoke (ADR-0058).

No external service and no secret: it originates messages via :meth:`inject` (as if a user
typed in a chat) and records the replies it is asked to :meth:`send`. It deliberately does
**not** re-inject a reply (that would loop the turn forever), so the full
inbound → turn → outbound path can be exercised end-to-end with no Telegram/Discord account.
It is the default provider, so a fresh install has a working bridge path out of the box.
"""

from __future__ import annotations

from epicurus_core import InboundMessage, OutboundMessage, get_logger
from epicurus_messaging.providers import InboundHandler

log = get_logger("messaging.loopback")

LOOPBACK_BRIDGE = "loopback"


class LoopbackProvider:
    """In-process echo bridge. Implements :class:`~epicurus_messaging.providers.BridgeProvider`."""

    def __init__(self, *, bridge: str = LOOPBACK_BRIDGE) -> None:
        self._bridge = bridge
        self._on_inbound: InboundHandler | None = None
        # Replies handed to send(), kept for inspection (tests + the /status surface).
        self.sent: list[OutboundMessage] = []

    def provider_name(self) -> str:
        return self._bridge

    def secret_names(self) -> list[str]:
        return []  # in-process; no credential

    def connected(self) -> bool:
        return True  # in-process echo bridge — always live once constructed

    async def start(self, on_inbound: InboundHandler) -> None:
        self._on_inbound = on_inbound

    async def send(self, message: OutboundMessage) -> None:
        # "Delivery" for loopback is just recording the reply — never re-inject (would loop).
        self.sent.append(message)
        log.info("loopback delivered reply", channel=message.channel_id, chars=len(message.text))

    async def stop(self) -> None:
        self._on_inbound = None

    async def inject(
        self,
        *,
        tenant: str,
        channel_id: str,
        text: str,
        thread_id: str | None = None,
        sender_id: str = "",
        sender_name: str = "",
        provider_msg_id: str = "",
    ) -> InboundMessage:
        """Originate an inbound message as if it arrived from the bridge (dev/test entrypoint).

        Returns the message it published so callers can assert on it. Raises if the provider
        has not been started (no inbound handler wired yet).
        """
        if self._on_inbound is None:
            raise RuntimeError("loopback provider not started; call start() first")
        message = InboundMessage(
            tenant=tenant,
            bridge=self._bridge,
            channel_id=channel_id,
            thread_id=thread_id,
            sender_id=sender_id,
            sender_name=sender_name,
            text=text,
            provider_msg_id=provider_msg_id,
        )
        await self._on_inbound(message)
        return message
