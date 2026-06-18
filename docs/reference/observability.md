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

## Live log stream (ADR-0030)

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
