"""Integration tests for the NATS EventBus. Require Docker (testcontainers)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Sequence

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from structlog.testing import capture_logs
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from epicurus_core.events import Event, EventBus, Payload
from epicurus_core.tracing import TENANT_ATTRIBUTE

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def nats_url() -> Iterator[str]:
    container = DockerContainer("nats:2.10").with_command("-js").with_exposed_ports(4222)
    with container:
        wait_for_logs(container, "Server is ready")
        host = container.get_container_host_ip()
        port = container.get_exposed_port(4222)
        yield f"nats://{host}:{port}"


async def _spans_until(
    exporter: InMemorySpanExporter,
    predicate: Callable[[Sequence[ReadableSpan]], bool],
    *,
    timeout: float = 3.0,
) -> Sequence[ReadableSpan]:
    """Poll the exporter until ``predicate`` holds — a consumer span finishes a beat
    after the test wakes, so we wait for it rather than racing on a fixed sleep."""
    for _ in range(int(timeout / 0.05)):
        spans = exporter.get_finished_spans()
        if predicate(spans):
            return spans
        await asyncio.sleep(0.05)
    return exporter.get_finished_spans()


def _named(spans: Sequence[ReadableSpan], name: str) -> ReadableSpan:
    matches = [s for s in spans if s.name == name]
    assert matches, f"no span named {name!r}; saw {[s.name for s in spans]}"
    return matches[-1]


async def test_publish_subscribe(nats_url: str) -> None:
    received: asyncio.Queue[Event] = asyncio.Queue()

    async with EventBus(nats_url) as bus:
        await bus.subscribe("demo.topic", received.put, tenant_id="acme")
        await bus.client.flush()
        await bus.publish("demo.topic", {"hello": "world"}, tenant_id="acme")
        event = await asyncio.wait_for(received.get(), timeout=2)

    assert event.subject == "acme.demo.topic"
    assert event.json() == {"hello": "world"}


async def test_request_reply(nats_url: str) -> None:
    async def echo(event: Event) -> bytes:
        return b"pong:" + event.data

    async with EventBus(nats_url) as bus:
        await bus.reply("demo.ping", echo, tenant_id="acme")
        await bus.client.flush()
        response = await bus.request("demo.ping", b"ping", tenant_id="acme")

    assert response.text == "pong:ping"


async def test_handler_exception_is_logged_and_subscription_survives(nats_url: str) -> None:
    received: asyncio.Queue[Event] = asyncio.Queue()

    async def flaky(event: Event) -> None:
        if event.data == b"boom":
            raise RuntimeError("boom")
        await received.put(event)

    async with EventBus(nats_url) as bus:
        await bus.subscribe("demo.flaky", flaky, tenant_id="acme")
        await bus.client.flush()
        with capture_logs() as logs:
            await bus.publish("demo.flaky", b"boom", tenant_id="acme")
            await bus.publish("demo.flaky", b"ok", tenant_id="acme")
            # Delivery is in-order: receiving "ok" proves "boom" was handled first.
            event = await asyncio.wait_for(received.get(), timeout=2)

    assert event.data == b"ok"
    assert any(entry["event"] == "event handler raised" for entry in logs)


async def test_replier_exception_is_logged_and_later_requests_succeed(nats_url: str) -> None:
    calls = 0

    async def flaky_replier(event: Event) -> Payload:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first call only")
        return b"recovered"

    async with EventBus(nats_url) as bus:
        await bus.reply("demo.flaky-reply", flaky_replier, tenant_id="acme")
        await bus.client.flush()

        # The raising replier sends no response — the requester times out.
        with capture_logs() as logs, pytest.raises(TimeoutError):
            await bus.request("demo.flaky-reply", b"one", timeout=0.5, tenant_id="acme")

        # The subscription survives and serves the next request.
        response = await bus.request("demo.flaky-reply", b"two", timeout=2, tenant_id="acme")

    assert response.data == b"recovered"
    assert any("replier raised" in entry["event"] for entry in logs)


async def test_tenant_isolation(nats_url: str) -> None:
    received: asyncio.Queue[Event] = asyncio.Queue()

    async with EventBus(nats_url) as bus:
        await bus.subscribe("demo.topic", received.put, tenant_id="acme")
        await bus.client.flush()

        # A different tenant's message must NOT reach the acme subscriber.
        await bus.publish("demo.topic", b"x", tenant_id="other")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(received.get(), timeout=0.5)

        # The same tenant's message is delivered.
        await bus.publish("demo.topic", b"y", tenant_id="acme")
        event = await asyncio.wait_for(received.get(), timeout=2)

    assert event.subject == "acme.demo.topic"
    assert event.data == b"y"


# ── Tracing (#57): spans, cross-bus propagation, redaction posture ─────────────


async def test_publish_and_handle_emit_linked_spans(
    nats_url: str, span_exporter: InMemorySpanExporter
) -> None:
    received: asyncio.Queue[Event] = asyncio.Queue()

    async with EventBus(nats_url) as bus:
        await bus.subscribe("demo.trace", received.put, tenant_id="acme")
        await bus.client.flush()
        await bus.publish("demo.trace", {"hello": "world"}, tenant_id="acme")
        await asyncio.wait_for(received.get(), timeout=2)
        spans = await _spans_until(
            span_exporter,
            lambda s: {"demo.trace publish", "demo.trace process"} <= {sp.name for sp in s},
        )

    publish_span = _named(spans, "demo.trace publish")
    process_span = _named(spans, "demo.trace process")
    assert publish_span.kind == SpanKind.PRODUCER
    assert process_span.kind == SpanKind.CONSUMER
    # The trace context rode the NATS headers across the bus: one trace, publish → handle.
    assert publish_span.context is not None and process_span.context is not None
    assert process_span.context.trace_id == publish_span.context.trace_id
    # Structural attributes only — never the payload.
    assert publish_span.attributes is not None
    assert publish_span.attributes["messaging.system"] == "nats"
    assert publish_span.attributes["messaging.destination.name"] == "demo.trace"
    assert publish_span.attributes[TENANT_ATTRIBUTE] == "acme"
    assert "world" not in str(dict(publish_span.attributes)), "payload must not leak into a span"


async def test_request_reply_emits_linked_client_server_spans(
    nats_url: str, span_exporter: InMemorySpanExporter
) -> None:
    async def echo(event: Event) -> bytes:
        return b"pong:" + event.data

    async with EventBus(nats_url) as bus:
        await bus.reply("demo.rr", echo, tenant_id="acme")
        await bus.client.flush()
        response = await bus.request("demo.rr", b"ping", tenant_id="acme")
        spans = await _spans_until(
            span_exporter,
            lambda s: {"demo.rr request", "demo.rr process"} <= {sp.name for sp in s},
        )

    assert response.text == "pong:ping"
    client_span = _named(spans, "demo.rr request")
    server_span = _named(spans, "demo.rr process")
    assert client_span.kind == SpanKind.CLIENT
    assert server_span.kind == SpanKind.SERVER
    assert client_span.context is not None and server_span.context is not None
    assert server_span.context.trace_id == client_span.context.trace_id


async def test_handler_exception_marks_span_error(
    nats_url: str, span_exporter: InMemorySpanExporter
) -> None:
    async def boom(_event: Event) -> None:
        raise RuntimeError("kaboom")

    async with EventBus(nats_url) as bus:
        await bus.subscribe("demo.boom", boom, tenant_id="acme")
        await bus.client.flush()
        with capture_logs():
            await bus.publish("demo.boom", b"x", tenant_id="acme")
            spans = await _spans_until(
                span_exporter, lambda s: any(sp.name == "demo.boom process" for sp in s)
            )

    process_span = _named(spans, "demo.boom process")
    assert process_span.status.status_code == StatusCode.ERROR
