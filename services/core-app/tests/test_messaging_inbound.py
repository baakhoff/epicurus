"""Tests for the core inbound-messaging consumer (ADR-0058).

The unit tests drive the consumer's logic with a fake bus + fake turn-runner (no
network, no LLM). The integration test boots a real NATS container and proves the full
inbound → (headless) turn → outbound path over the wire — the turn is faked because the
CI/smoke stack has no model, but the subscribe, session keying, and outbound publish are
all real.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from structlog.testing import capture_logs

from epicurus_core import (
    MESSAGING_INBOUND,
    MESSAGING_OUTBOUND,
    EventBus,
    InboundMessage,
    OutboundMessage,
)
from epicurus_core_app.agent.agent import AgentTurn
from epicurus_core_app.llm.models import ChatMessage
from epicurus_core_app.llm.power import PowerController
from epicurus_core_app.messaging import InboundConsumer


class _FakeRunner:
    """A stand-in agent: records each turn and returns a canned answer."""

    def __init__(self, answer: str = "the answer") -> None:
        self._answer = answer
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
    ) -> AgentTurn:
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "tenant_id": tenant_id,
                "session_id": session_id,
            }
        )
        return AgentTurn(content=self._answer, stopped="completed")


class _FakeBus:
    """Records publishes; ``subscribe`` is unused by the direct ``handle`` tests."""

    def __init__(self) -> None:
        self.published: list[tuple[str, Any, str | None]] = []

    async def publish(self, subject: str, data: Any, tenant_id: str | None = None) -> None:
        self.published.append((subject, data, tenant_id))


def _consumer(
    *,
    runner: _FakeRunner | None = None,
    bus: _FakeBus | None = None,
    power: PowerController | None = None,
    model: str | None = None,
) -> tuple[InboundConsumer, _FakeRunner, _FakeBus, PowerController]:
    runner = runner or _FakeRunner()
    bus = bus or _FakeBus()
    power = power or PowerController()
    consumer = InboundConsumer(
        bus=bus,  # type: ignore[arg-type]  # structural: only publish() is used here
        agent=runner,
        power=power,
        default_tenant="local",
        model=model,
    )
    return consumer, runner, bus, power


async def test_handle_runs_turn_and_publishes_reply() -> None:
    consumer, runner, bus, _ = _consumer(model="bridge-model")
    inbound = InboundMessage(
        tenant="local",
        bridge="telegram",
        channel_id="chat42",
        thread_id="t7",
        text="  hello there  ",
        provider_msg_id="m99",
    )
    await consumer.handle(inbound)

    # The turn ran once, keyed by the channel's session id, under the message's tenant.
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["session_id"] == "telegram:chat42:t7"
    assert call["tenant_id"] == "local"
    assert call["model"] == "bridge-model"
    assert [m.role for m in call["messages"]] == ["user"]
    assert call["messages"][0].content == "hello there"  # trimmed

    # The reply went out on the tenant-scoped outbound subject, routed back to the channel.
    assert len(bus.published) == 1
    subject, data, tenant_id = bus.published[0]
    assert subject == MESSAGING_OUTBOUND
    assert tenant_id == "local"
    out = OutboundMessage.model_validate(data)
    assert out.bridge == "telegram"
    assert out.channel_id == "chat42"
    assert out.thread_id == "t7"
    assert out.text == "the answer"
    assert out.reply_to_msg_id == "m99"


async def test_handle_skips_when_paused() -> None:
    power = PowerController()
    power.pause()
    consumer, runner, bus, _ = _consumer(power=power)
    with capture_logs() as logs:
        await consumer.handle(
            InboundMessage(tenant="local", bridge="loopback", channel_id="c", text="hi")
        )
    assert runner.calls == []
    assert bus.published == []
    assert any("paused" in entry.get("event", "") for entry in logs)


async def test_handle_skips_empty_text() -> None:
    consumer, runner, bus, _ = _consumer()
    await consumer.handle(
        InboundMessage(tenant="local", bridge="loopback", channel_id="c", text="   ")
    )
    assert runner.calls == []
    assert bus.published == []


async def test_handle_drops_invalid_tenant() -> None:
    consumer, runner, bus, _ = _consumer()
    await consumer.handle(
        InboundMessage(tenant="Not A Tenant!", bridge="loopback", channel_id="c", text="hi")
    )
    assert runner.calls == []
    assert bus.published == []


async def test_handle_event_drops_unparseable_payload() -> None:
    consumer, runner, _, _ = _consumer()

    class _Event:
        subject = "local.messaging.inbound"

        def json(self) -> Any:
            return {"not": "an inbound message"}  # missing required fields

    with capture_logs() as logs:
        await consumer._handle_event(_Event())  # type: ignore[arg-type]
    assert runner.calls == []
    assert any("unparseable" in entry.get("event", "") for entry in logs)


# ── integration: the real wire (NATS), faked turn ───────────────────────────────────────
pytestmark_integration = pytest.mark.integration


@pytest.fixture(scope="module")
def nats_url() -> Iterator[str]:
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    container = DockerContainer("nats:2.10").with_command("-js").with_exposed_ports(4222)
    with container:
        wait_for_logs(container, "Server is ready")
        yield f"nats://{container.get_container_host_ip()}:{container.get_exposed_port(4222)}"


@pytest.mark.integration
async def test_inbound_to_outbound_over_nats(nats_url: str) -> None:
    """Publish an inbound message → the consumer runs a (faked) turn → an outbound reply
    arrives on ``messaging.outbound``, routed back to the originating channel."""
    runner = _FakeRunner(answer="hi from the agent")
    power = PowerController()
    received: list[OutboundMessage] = []
    done = asyncio.Event()

    async with EventBus(nats_url) as bus:
        consumer = InboundConsumer(bus=bus, agent=runner, power=power, default_tenant="local")
        await consumer.start()

        async def _collect(event: Any) -> None:
            received.append(OutboundMessage.model_validate(event.json()))
            done.set()

        await bus.subscribe(MESSAGING_OUTBOUND, _collect, tenant_id="local")
        await bus.client.flush()

        await bus.publish(
            MESSAGING_INBOUND,
            InboundMessage(
                tenant="local",
                bridge="loopback",
                channel_id="room1",
                text="ping",
                provider_msg_id="m1",
            ).model_dump(),
            tenant_id="local",
        )
        await asyncio.wait_for(done.wait(), timeout=10)
        await consumer.stop()

    assert len(runner.calls) == 1
    assert runner.calls[0]["session_id"] == "loopback:room1"
    assert len(received) == 1
    assert received[0].text == "hi from the agent"
    assert received[0].channel_id == "room1"
    assert received[0].reply_to_msg_id == "m1"
