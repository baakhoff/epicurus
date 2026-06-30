# Reference: `observability`

`epicurus_core.observability` — the shared operational HTTP surface every service
exposes: `/health` and `/metrics`.

## `create_ops_router`

```python
def create_ops_router(service_name: str, *, version: str = __version__, registry: CollectorRegistry = REGISTRY) -> fastapi.APIRouter
```

Build a router exposing:

- **`GET /health`** — returns a [`HealthResponse`](#healthresponse). `version`
  defaults to the `epicurus-core` version; pass the service's own (e.g. from
  `importlib.metadata.version`) so `/health` reports the service version.
- **`GET /metrics`** — Prometheus exposition for `registry` (defaults to the
  process-wide registry).

## `add_ops_routes`

```python
def add_ops_routes(app: fastapi.FastAPI, service_name: str, *, version: str = __version__, registry: CollectorRegistry = REGISTRY) -> None
```

Mount the ops router onto an existing FastAPI app.

## `HealthResponse`

Pydantic model returned by `GET /health`:

`status: str` · `service: str` · `version: str`.

### Example

```python
from fastapi import FastAPI
from epicurus_core import add_ops_routes

app = FastAPI()
add_ops_routes(app, service_name="greeter")
# GET /health  -> {"status": "ok", "service": "greeter", "version": "..."}
# GET /metrics -> Prometheus text exposition
```

---

## Tracing (#57, ADR-0068)

`epicurus_core.tracing` — the observability stack's third signal. Distributed traces
to **Tempo** over OTLP/HTTP, covering FastAPI requests and the NATS `EventBus`.

**Off by default.** The lean stack pays nothing; disabled tracing is a runtime no-op
(no provider, no exporter, and `EventBus` spans degrade to cheap no-ops). Turn it on
fleet-wide by setting the env below and bringing the stack up with the `observability`
profile (so Tempo is listening), then explore traces in Grafana's Tempo datasource.

### `setup_tracing`

```python
def setup_tracing(app: fastapi.FastAPI, settings: CoreSettings, *, version: str = __version__) -> bool
```

Call once in a service's `create_app`, right after `add_ops_routes`. When
`settings.otel_traces_enabled` it installs the global `TracerProvider` + OTLP/HTTP
exporter (once per process) and instruments the FastAPI app — **excluding `/health` and
`/metrics`** (polled constantly, they would drown real spans). Returns whether tracing
was set up (`False` when disabled). Idempotent and safe to call from every service. The
service template and echo already call it, so a new module traces with no extra code.

```python
from epicurus_core import add_ops_routes, setup_tracing

app = FastAPI()
add_ops_routes(app, service_name="greeter", version=version)
setup_tracing(app, settings, version=version)  # no-op unless OTEL_TRACES_ENABLED
```

### `get_tracer`

```python
def get_tracer(name: str) -> opentelemetry.trace.Tracer
```

A tracer for custom spans inside a module. Before `setup_tracing` runs it is the OTel
no-op tracer, so callers can open spans unconditionally.

### What is traced

- **HTTP** — every route on an instrumented app gets a `SERVER` span (method, route,
  status), tagged with the current tenant (`epicurus.tenant`), `/health` + `/metrics`
  excluded.
- **`EventBus`** — `publish` (`PRODUCER`), `request` (`CLIENT`), and each delivered
  handler / replier (`CONSUMER` / `SERVER`). The trace context rides in NATS message
  headers (W3C `traceparent`), so a consumer span links to the publisher's — one trace
  across the bus. Span attributes: `messaging.system=nats`, `messaging.operation`,
  `messaging.destination.name` (the base subject), `messaging.message.body.size`, and
  `epicurus.tenant`.

### Redaction posture

Spans carry **no** payloads, message bodies, request/response bodies, headers, or
prompt content — only structure (method, route, subject, tenant, byte size). There is
nothing to redact because nothing sensitive is recorded, the same stance the logs take.

### Tenant (constraint #1)

The process-level resource carries the service's **default** tenant (self-host is
single-tenant); `EventBus` spans additionally tag the **operation's** tenant, known at
publish / subscribe time. A future multi-tenant SaaS build moves the request tenant onto
the server span once it is resolved per request.

### Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `OTEL_TRACES_ENABLED` | `false` | Master on/off. `true` installs the provider + exporter and instruments the app. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://tempo:4318` | OTLP/HTTP **base** URL; the exporter appends `/v1/traces`. Tempo's HTTP receiver on the internal Docker network. |

```sh
OTEL_TRACES_ENABLED=true docker compose --profile observability up -d
```

The OTLP/HTTP exporter is used deliberately — it avoids the heavy native `grpcio`
dependency the gRPC exporter pulls in (Tempo accepts OTLP over HTTP on `:4318` all the
same).

---

## Live log stream (ADR-0031)

`core-app` exposes a real-time structured log stream via SSE at:

```
GET /platform/v1/logs/stream
```

This is a **core-app–only** endpoint (not on every service). It taps directly into
the structlog processor chain via a ring-buffer processor injected into
`configure_logging(extra_processors=[...])`.

### Query parameters

| Parameter | Type   | Default | Description |
|-----------|--------|---------|-------------|
| `level`   | string | `info`  | Minimum level to emit: `debug` / `info` / `warning` / `error` / `critical`. |
| `service` | string | —       | Optional prefix filter on the `service` field (e.g. `epicurus_core_app.agent`). |

### SSE event format

Each frame has `event: log` and `data` containing a JSON `LogEntry`:

```json
{
  "ts": "2026-06-18T12:00:00.123456Z",
  "level": "info",
  "service": "epicurus_core_app.agent.routes",
  "message": "chat turn started",
  "context": { "session_id": "abc123", "tenant": "local" }
}
```

`context` contains the remaining structlog event-dict fields. Keys whose name
contains `token`, `key`, `secret`, `password`, `credential`, or `auth` are
**stripped** before any entry leaves the buffer.

### Behaviour

- The server replays up to **200** buffered history entries first (so a freshly
  opened tab gets recent context), then streams live entries.
- Each subscription holds an asyncio Queue (maxsize 500). A slow consumer drops
  frames rather than back-pressuring the logger.
- The stream never closes on its own — clients reconnect after any disconnect.

### `configure_logging` signature (epicurus-core ≥ 0.9.0)

```python
def configure_logging(
    settings: CoreSettings,
    extra_processors: list[structlog.typing.Processor] | None = None,
) -> None: ...
```

`extra_processors` are inserted after the shared chain and **before** the
renderer, so they see the full, structured event dict.

### Web surface

The Observability screen (`/observability`) renders a live log console backed by
this endpoint. It:

- Replays the history buffer on connect.
- Reconnects automatically on disconnect (3 s back-off).
- Filters by minimum level and service prefix without a page reload.
- Supports context expansion (click ▼ on a log row with extra fields).
- Shows a health summary from `GET /platform/v1/readiness` at the top.
