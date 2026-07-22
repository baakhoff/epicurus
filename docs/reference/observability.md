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

That rule now lives in `epicurus_core.redaction` rather than privately in `log_stream`
(ADR-0103 §6): the [events feed](#raw-events-feed) is its second surface, and a security
rule kept in two places is one that drifts. Behaviour here is unchanged — same list, same
blunt case-insensitive substring match on key *names*, one source.

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

---

## Raw events feed (ADR-0103)

The second live feed: what the *modules* announced happened, as recorded in the core's
durable `module_events` log. Where the log stream is the core narrating itself, this is the
world changing — mail arriving, an event moving. See [events](events.md) for the envelope,
the emit helper, and the catalog.

```
GET /platform/v1/events/stream     # SSE tail
GET /platform/v1/events            # the same data as a plain page
```

Both are **core-app–only**. Unlike the log stream (an in-memory ring buffer), these read a
Postgres table, so history survives a restart.

### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `tenant_id` | string | the default tenant | Which tenant's events to read. |
| `module` | string | — | Exact module filter (e.g. `mail`). |
| `type` | string | — | Exact event-type filter (e.g. `mail.received`). |
| `limit` | int | `200` | Snapshot endpoint only; 1–1000. |

### SSE event format

Each frame has `event: module_event` and `data` containing a JSON `LoggedEvent`:

```json
{
  "id": 42,
  "tenant": "local",
  "module": "mail",
  "type": "mail.received",
  "occurred_at": "2026-07-17T12:00:00Z",
  "received_at": "2026-07-17T12:00:01Z",
  "dedup_key": "gmail:18f2c1",
  "entity_ref": { "ref_id": "18f2c1", "module": "mail", "kind": "message", "title": "Re: lunch" },
  "payload": { "message_id": "18f2c1", "unread": 1 },
  "schema_version": 1
}
```

`occurred_at` is the emitter's clock (when the change happened); `received_at` is the
core's (when it heard about it) — not the same thing, and the feed orders by the latter.

### Behaviour

- History replays **oldest-first** (up to 200), then live events follow.
- The subscriber queue registers *before* the history query, so an event landing mid-replay
  is queued rather than lost. It may then appear twice; clients de-duplicate on `id`. A
  duplicated row is cosmetic, a missing one is not.
- Each subscription holds an asyncio Queue (maxsize 500); a slow consumer drops frames.
- The payload is safe to render verbatim: credential-shaped keys are rejected at emit and
  redacted again here (rows outlive the rule that let them in).
- The stream never closes on its own — clients reconnect after any disconnect.

---

## Automation runs feed (#669)

The third live feed: what the automations engine **did** about the world changing — fire →
filter verdict → run (model, tokens, duration) → sinks delivered / error, straight from the
`automation_runs` ledger ([automations](automations.md#the-run-ledger)). Skips are
first-class: a rate-capped or paused run appears with its *why* in `error`, because a cap
being hit should be visible, not inferred from silence.

```
GET /platform/v1/automations/runs/stream   # SSE tail
GET /platform/v1/automations/runs          # the same data as a plain page
```

### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `tenant_id` | string | the default tenant | Which tenant's runs to read. |
| `automation_id` | string | — | Exact automation filter. |
| `outcome` | string | — | `ok` · `error` · `skipped` (400 on anything else). |
| `limit` | int | `100` | Snapshot endpoint only; 1–500. |

### SSE event format

Each frame is `event: automation_run` with an `AutomationRunView` JSON body — the ledger
entry plus `trigger_entity_refs`: the triggering events' `EntityRef`s, resolved server-side
from the event log by row id so the feed renders source-entity hover-card chips with no
per-module code (empty for schedule/manual runs, and for trigger events retention has since
pruned).

### Behaviour

Identical to the events feed: history replays oldest-first (up to 200) with the subscriber
queue registered first (clients de-duplicate on `id`), a slow consumer drops frames past a
500-deep queue, and the stream never closes on its own. The live half is fed by the
runner's `on_recorded` hook the moment a ledger entry is written — at **every** autonomy
level, so even a `silent_act` run is visible here (the ledger and this feed are its only
trace).

---

## Web surface

The Observability screen (`/observability`) shows a health summary from
`GET /platform/v1/readiness`, then a tab strip over the core's live feeds. Only the visible
tab's console is mounted, so a hidden tab holds no open subscription.

**Logs** — the live log console backed by `/platform/v1/logs/stream`. It replays the
history buffer on connect, filters by minimum level and service prefix without a page
reload, and expands a row's `context` on click (▼).

**Events** — the raw events feed backed by `/platform/v1/events/stream`. It replays recent
history, filters by module and event type, shows each row's `entity_ref` title, and expands
a row's `payload` on click (▼).

**Automation runs** (#669) — the run ledger backed by `/platform/v1/automations/runs/stream`.
Each row reads fire → verdict → outcome (a skip's *why* inline) → model, tokens, duration,
sinks fired, with the triggering events' `EntityRef` hover-card chips beneath and the run's
`output` expandable on click (▼). Filterable by automation and outcome (server-side, they
re-subscribe) and by trigger module (a client-side view over the automations list — a run
itself carries no module — so switching it never tears the stream down).

All three reconnect automatically on disconnect (3 s back-off) via the shared `useSseFeed`
hook, cap the DOM at 500 entries, and follow the tail only while the reader is already at
it — scrolling up to read something is not yanked back by the next arriving entry.
