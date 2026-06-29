"""The inbound-message consumer — the first inbound NATS subscriber in core (ADR-0058).

It bridges an external channel to the agent. On a message published to
``messaging.inbound`` it: maps the channel to a persisted ``session_id``, runs a
**headless** agent turn (the same :meth:`Agent.run` the web uses — no SSE, the final
answer is collected and persisted like any turn), and publishes the answer as an
:class:`~epicurus_core.OutboundMessage` on ``messaging.outbound`` for the ``messaging``
module to deliver via the active bridge.

Memory and facts are tenant-scoped, so a bridge conversation shares the one brain with the
web UI — chatting from Telegram remembers what you told the assistant in the browser.

Power state is respected (ADR-0005): while the runtime is **paused**, GPU work is refused,
so an inbound message is **skipped** (the user resends once resumed) rather than queued —
the same degrade the web turn gets (a 503 there). Every message is handled defensively: a
malformed payload or a failed turn is logged and dropped, never breaking the subscription.

**v1 is single-tenant by subscription.** The consumer subscribes under the configured
default tenant (``<tenant>.messaging.inbound``); the per-message ``tenant`` then drives the
turn and the outbound publish. Fanning out across tenants (a wildcard subscription, or one
subscription per active tenant) is the multi-tenant follow-up — the per-message tenant
threading is already in place for it (constraint #1).
"""

from __future__ import annotations

from typing import Protocol

from nats.aio.subscription import Subscription
from pydantic import ValidationError

from epicurus_core import (
    MESSAGING_INBOUND,
    MESSAGING_OUTBOUND,
    EventBus,
    InboundMessage,
    OutboundMessage,
    get_logger,
    is_valid_tenant_id,
)
from epicurus_core.events import Event
from epicurus_core_app.agent.agent import AgentTurn
from epicurus_core_app.llm.models import ChatMessage
from epicurus_core_app.llm.power import PowerController

log = get_logger("epicurus_core_app.messaging")


class TurnRunner(Protocol):
    """The slice of the agent the consumer needs: run one headless turn to completion.

    :class:`~epicurus_core_app.agent.agent.Agent` satisfies this structurally; the Protocol
    keeps the consumer decoupled from the full agent for testing.
    """

    async def run(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
    ) -> AgentTurn: ...


class InboundConsumer:
    """Subscribes ``messaging.inbound`` → headless turn → publishes ``messaging.outbound``."""

    def __init__(
        self,
        *,
        bus: EventBus,
        agent: TurnRunner,
        power: PowerController,
        default_tenant: str,
        model: str | None = None,
    ) -> None:
        self._bus = bus
        self._agent = agent
        self._power = power
        self._default_tenant = default_tenant
        # Optional dedicated model for bridge turns; None → the gateway resolves the default.
        self._model = model
        self._sub: Subscription | None = None

    async def start(self) -> None:
        """Subscribe to the tenant-scoped inbound subject (idempotent)."""
        if self._sub is not None:
            return
        self._sub = await self._bus.subscribe(
            MESSAGING_INBOUND, self._handle_event, tenant_id=self._default_tenant
        )
        log.info("inbound messaging consumer subscribed", tenant=self._default_tenant)

    async def stop(self) -> None:
        """Unsubscribe (best-effort) so shutdown is clean."""
        if self._sub is None:
            return
        try:
            await self._sub.unsubscribe()
        except Exception as exc:  # draining/closed already — never fail shutdown on it
            log.warning("inbound consumer unsubscribe failed", error=str(exc))
        finally:
            self._sub = None

    async def _handle_event(self, event: Event) -> None:
        """Parse the NATS payload into an :class:`InboundMessage`, then handle it.

        A malformed payload is logged and dropped with a clean warning (rather than a
        traceback from the bus wrapper) so one bad message never stalls the subscription.
        """
        try:
            inbound = InboundMessage.model_validate(event.json())
        except (ValidationError, ValueError) as exc:
            log.warning(
                "dropping unparseable inbound message", subject=event.subject, error=str(exc)
            )
            return
        await self.handle(inbound)

    async def handle(self, inbound: InboundMessage) -> None:
        """Run the turn for one inbound message and route the reply back out.

        Skips when the runtime is paused, when the tenant is malformed, or when there is no
        text to answer (an attachment-only message, until attachments are promoted — ADR-0058).
        """
        if self._power.paused:
            log.info(
                "inbound message skipped: runtime paused",
                bridge=inbound.bridge,
                channel=inbound.channel_id,
            )
            return
        if not is_valid_tenant_id(inbound.tenant):
            log.warning("dropping inbound message with invalid tenant", tenant=inbound.tenant)
            return
        text = inbound.text.strip()
        if not text:
            log.info(
                "inbound message has no text to answer; skipping",
                bridge=inbound.bridge,
                channel=inbound.channel_id,
            )
            return

        session_id = inbound.session_id()
        log.info(
            "running headless turn for inbound message",
            bridge=inbound.bridge,
            channel=inbound.channel_id,
            session=session_id,
        )
        turn = await self._agent.run(
            [ChatMessage(role="user", content=text)],
            model=self._model,
            tenant_id=inbound.tenant,
            session_id=session_id,
        )
        await self._publish_reply(inbound, turn.content)

    async def _publish_reply(self, inbound: InboundMessage, text: str) -> None:
        outbound = OutboundMessage(
            tenant=inbound.tenant,
            bridge=inbound.bridge,
            channel_id=inbound.channel_id,
            thread_id=inbound.thread_id,
            text=text,
            # Thread the reply under the user's message when the provider can quote.
            reply_to_msg_id=inbound.provider_msg_id or None,
        )
        await self._bus.publish(MESSAGING_OUTBOUND, outbound.model_dump(), tenant_id=inbound.tenant)
