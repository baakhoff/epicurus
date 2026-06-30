"""Shared OpenTelemetry test harness for the epicurus-core suite (#57).

Installs one in-memory span exporter as the process-global tracer provider so tracing
tests can assert on emitted spans without reaching the network. OpenTelemetry allows the
provider to be set only once per process, so it is session-scoped and the exporter is
cleared around each test that uses it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture(scope="session")
def _otel_in_memory() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "epicurus-core-tests"}))
    # SimpleSpanProcessor exports synchronously on span end, so finished spans are
    # visible the instant a `with` block (or callback) exits — no flush race.
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture
def span_exporter(_otel_in_memory: InMemorySpanExporter) -> Iterator[InMemorySpanExporter]:
    """An in-memory span exporter, cleared around the test (#57)."""
    _otel_in_memory.clear()
    yield _otel_in_memory
    _otel_in_memory.clear()
