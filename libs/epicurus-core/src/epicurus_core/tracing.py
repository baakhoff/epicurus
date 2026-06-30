"""OpenTelemetry tracing — the observability stack's third signal (#57, ADR-0068).

Optional, env-driven distributed tracing for every epicurus service. When enabled,
FastAPI requests and :class:`~epicurus_core.events.EventBus` publish / request / handle
operations emit spans, exported to Tempo over OTLP/HTTP. **Disabled by default:** the
lean stack (no ``observability`` compose profile) pays nothing, and the EventBus
instrumentation degrades to no-op spans that cost a couple of function calls.

Posture (mirrors the logging redaction stance): spans carry **no** payloads, message
bodies, headers, or prompt content — only structural attributes (HTTP method / route /
status from the FastAPI instrumentation, NATS subject, tenant, byte sizes). There is
nothing to redact because nothing sensitive is ever recorded.

Tenant (constraint #1): the process-level *resource* carries the service's default
tenant (self-host is single-tenant), and EventBus spans tag the **operation's** tenant
— the real per-message tenant, known at publish / subscribe time. A future multi-tenant
SaaS build moves the request tenant onto the server span once it is resolved per request.

Only the OpenTelemetry **api** is imported at module load (it is cheap and the EventBus
needs it regardless); the SDK, the OTLP exporter, and the FastAPI instrumentation are
imported lazily inside :func:`setup_tracing`, so importing ``epicurus_core`` with tracing
off never pulls protobuf / requests / the asgi middleware chain.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from opentelemetry import propagate, trace

from epicurus_core._version import __version__
from epicurus_core.config import CoreSettings
from epicurus_core.logging import get_logger
from epicurus_core.tenancy import TenantError, current_tenant

if TYPE_CHECKING:
    from fastapi import FastAPI
    from opentelemetry.context import Context
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.trace import Span, Tracer

__all__ = [
    "EVENT_TRACER_NAME",
    "TENANT_ATTRIBUTE",
    "extract_trace_context",
    "get_tracer",
    "inject_trace_headers",
    "setup_tracing",
]

log = get_logger("epicurus_core.tracing")

#: Tracer name for EventBus spans (also used by tests to scope assertions).
EVENT_TRACER_NAME = "epicurus_core.events"

#: Custom span/resource attribute key carrying the epicurus tenant (constraint #1).
TENANT_ATTRIBUTE = "epicurus.tenant"

# URL path substrings the FastAPI instrumentation must NOT trace: the ops surface is
# polled constantly by Docker / Prometheus and would drown real spans in noise.
_EXCLUDED_URLS = "health,metrics"


def get_tracer(name: str) -> Tracer:
    """Return a tracer for ``name``.

    Before :func:`setup_tracing` installs a provider this is the api's no-op tracer, so
    callers (e.g. the EventBus) can instrument unconditionally: spans become cheap
    no-ops when tracing is disabled, with no ``if enabled`` branching at the call site.
    """
    return trace.get_tracer(name)


def inject_trace_headers() -> dict[str, str]:
    """Serialize the active trace context into a fresh carrier (W3C ``traceparent``).

    Returns an empty dict when there is no recording span (tracing disabled, or called
    outside any span), so callers can pass ``headers or None`` to NATS unchanged.
    """
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract_trace_context(headers: Mapping[str, str] | None) -> Context | None:
    """Rebuild the parent context from inbound carrier ``headers``.

    Returns ``None`` when there is nothing to extract, so the consumer span starts a
    fresh trace rather than an orphaned child.
    """
    if not headers:
        return None
    return propagate.extract(dict(headers))


def setup_tracing(app: FastAPI, settings: CoreSettings, *, version: str = __version__) -> bool:
    """Wire OpenTelemetry tracing for ``app`` when ``settings.otel_traces_enabled``.

    Idempotent and safe to call from every service's ``create_app`` right after
    ``add_ops_routes``. Installs the global ``TracerProvider`` + OTLP/HTTP exporter
    (once per process), then instruments the FastAPI app — excluding ``/health`` and
    ``/metrics``. Returns whether tracing was set up (``False`` when disabled) so the
    caller can log it.

    ``version`` is the service's own distribution version (e.g. from
    ``importlib.metadata.version``), recorded as ``service.version`` on the resource.
    """
    if not settings.otel_traces_enabled:
        return False
    _ensure_provider(settings, version)
    # Lazy: importing the instrumentation pulls the asgi / util-http chain, only needed
    # when tracing is enabled.
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=trace.get_tracer_provider(),
        excluded_urls=_EXCLUDED_URLS,
        server_request_hook=_server_request_hook,
    )
    return True


def _ensure_provider(settings: CoreSettings, version: str) -> None:
    """Install the global SDK ``TracerProvider`` once.

    No-op if an SDK provider is already set — so a second service in one process, or a
    test that pre-installs an in-memory provider, is respected (and tests export to
    memory instead of reaching for the network).
    """
    from opentelemetry.sdk.trace import TracerProvider

    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return
    # Lazy: the OTLP/HTTP exporter pulls protobuf + requests, only needed once tracing
    # is actually switched on.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    endpoint = _traces_endpoint(settings.otel_exporter_otlp_endpoint)
    provider = TracerProvider(resource=_build_resource(settings, version))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    log.info("tracing enabled", endpoint=endpoint, service=settings.service_name)


def _build_resource(settings: CoreSettings, version: str) -> Resource:
    """The process-identity resource stamped on every span (string keys keep this
    independent of semantic-convention package churn)."""
    from opentelemetry.sdk.resources import Resource

    return Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": version,
            "deployment.environment": settings.app_env,
            TENANT_ATTRIBUTE: settings.default_tenant_id,
        }
    )


def _traces_endpoint(base: str) -> str:
    """The OTLP/HTTP traces URL — ``<base>/v1/traces`` — appending the per-signal path
    the OTLP spec defines for the base endpoint."""
    return f"{base.rstrip('/')}/v1/traces"


def _server_request_hook(span: Span, scope: dict[str, Any]) -> None:
    """Tag the server span with the current tenant, best-effort.

    Self-host binds a single tenant for the whole process, so this resolves to the
    default; it is guarded because :func:`current_tenant` raises when none is bound.
    """
    if not span.is_recording():
        return
    # current_tenant() raises when none is bound (e.g. before per-request resolution).
    with contextlib.suppress(TenantError):
        span.set_attribute(TENANT_ATTRIBUTE, current_tenant())
