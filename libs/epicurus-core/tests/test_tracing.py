"""Unit tests for OpenTelemetry tracing setup + propagation helpers (#57).

These exercise everything that does not need a live NATS bus: the enable/disable gate,
FastAPI instrumentation (incl. excluding the ops surface), the resource attributes, the
OTLP endpoint shape, and W3C context inject/extract. EventBus span emission and
cross-bus propagation are covered against real NATS in ``test_events.py``.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from epicurus_core import add_ops_routes, setup_tracing
from epicurus_core.config import CoreSettings
from epicurus_core.tracing import (
    TENANT_ATTRIBUTE,
    _build_resource,
    _traces_endpoint,
    extract_trace_context,
    get_tracer,
    inject_trace_headers,
)


def _app_with_ping() -> FastAPI:
    app = FastAPI()
    add_ops_routes(app, service_name="svc")

    @app.get("/ping")
    def ping() -> dict[str, bool]:
        return {"pong": True}

    return app


def test_setup_tracing_disabled_is_a_noop() -> None:
    settings = CoreSettings(service_name="svc", otel_traces_enabled=False)
    app = _app_with_ping()
    assert setup_tracing(app, settings) is False
    # The FastAPI instrumentor stamps this attribute on an app it instruments.
    assert getattr(app, "_is_instrumented_by_opentelemetry", False) is False


def test_setup_tracing_enabled_instruments_and_excludes_ops(
    span_exporter: InMemorySpanExporter,
) -> None:
    settings = CoreSettings(service_name="svc", otel_traces_enabled=True)
    app = _app_with_ping()
    assert setup_tracing(app, settings) is True

    client = TestClient(app)
    client.get("/ping")
    after_ping = span_exporter.get_finished_spans()
    assert any(s.kind == SpanKind.SERVER for s in after_ping), "a server span for /ping"

    # /health and /metrics are excluded — polled constantly, they would drown real spans.
    client.get("/health")
    client.get("/metrics")
    assert len(span_exporter.get_finished_spans()) == len(after_ping)


def test_setup_tracing_is_idempotent(span_exporter: InMemorySpanExporter) -> None:
    settings = CoreSettings(service_name="svc", otel_traces_enabled=True)
    # A second service in the same process must not blow up re-installing the provider.
    assert setup_tracing(_app_with_ping(), settings) is True
    assert setup_tracing(_app_with_ping(), settings) is True


def test_traces_endpoint_appends_signal_path() -> None:
    assert _traces_endpoint("http://tempo:4318") == "http://tempo:4318/v1/traces"
    # A trailing slash on the base must not double up.
    assert _traces_endpoint("http://tempo:4318/") == "http://tempo:4318/v1/traces"


def test_build_resource_carries_identity_and_tenant() -> None:
    settings = CoreSettings(service_name="calendar", default_tenant_id="acme", app_env="staging")
    attrs = _build_resource(settings, "1.2.3").attributes
    assert attrs["service.name"] == "calendar"
    assert attrs["service.version"] == "1.2.3"
    assert attrs["deployment.environment"] == "staging"
    assert attrs[TENANT_ATTRIBUTE] == "acme"


def test_inject_extract_roundtrip_links_traces(span_exporter: InMemorySpanExporter) -> None:
    tracer = get_tracer("test")
    with tracer.start_as_current_span("parent") as parent:
        carrier = inject_trace_headers()
        parent_trace_id = parent.get_span_context().trace_id
    assert "traceparent" in carrier

    ctx = extract_trace_context(carrier)
    assert ctx is not None
    with tracer.start_as_current_span("child", context=ctx) as child:
        # The child rebuilt from the carrier belongs to the parent's trace.
        assert child.get_span_context().trace_id == parent_trace_id


def test_inject_without_active_span_is_empty(span_exporter: InMemorySpanExporter) -> None:
    # No active span → nothing to propagate → callers pass `headers or None` unchanged.
    assert inject_trace_headers() == {}


def test_extract_none_or_empty_returns_none() -> None:
    assert extract_trace_context(None) is None
    assert extract_trace_context({}) is None
