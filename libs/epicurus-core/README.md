# epicurus-core

Shared contract and runtime used by every epicurus service.

## Available now

- **`config`** — `CoreSettings` (pydantic-settings): env-driven, non-secret
  configuration shared by every service. Subclass to add service fields.
- **`logging`** — `configure_logging` / `get_logger`: structlog, console in local
  dev and JSON otherwise, with contextvar-based correlation.
- **`tenancy`** — the dual-track primitive. Scopes every NATS subject, Qdrant
  collection, OpenBao secret path, and object bucket by tenant, plus a
  contextvar-bound "current tenant".
- **`observability`** — `add_ops_routes` / `create_ops_router`: the shared
  `GET /health` + `GET /metrics` (Prometheus) surface.
- **`events`** — `EventBus`: async NATS client (the event backbone). Tenant-scoped
  `publish` / `subscribe` / `request` / `reply`, plus the JetStream surface the module
  event spine is durable on (`jetstream` / `ensure_stream` / `pull_subscribe_any_tenant`).
- **`module_events`** — `emit_event` and the `EventEnvelope` contract, with the spine's
  stream/durable/retention constants (`EVENTS_STREAM`, `EVENTS_DURABLE`, …). Delivery is
  at-least-once from the NATS server to the core's durable log; see the module docstring
  for what the publish side does *not* promise.
- **`tracing`** — `setup_tracing` / `get_tracer`: optional OpenTelemetry distributed
  tracing to Tempo (OTLP/HTTP), covering FastAPI requests + the `EventBus` (trace
  context propagates across NATS). Env-driven on/off (`OTEL_TRACES_ENABLED`), a no-op
  when disabled.
- **`module`** — `EpicurusModule`: the MCP module base (wraps `FastMCP`). Register
  tools, declare emitted/consumed events, serve over HTTP (`http_app()`), and
  generate the **manifest**.
- **`manifest`** — `ModuleManifest` / `ToolSpec` / `EventSpec` + `CONTRACT_VERSION`:
  the descriptor a module ships (ADR-0004); basis for the template and installer.
- **`secret_store`** — `SecretStore`: tenant-scoped secret access in OpenBao
  (KV v2, via `hvac`); `get` / `set` / `delete`.

## Usage

```python
from epicurus_core import (
    CoreSettings,
    add_ops_routes,
    configure_logging,
    get_logger,
    scope_subject,
    set_current_tenant,
)

settings = CoreSettings()
configure_logging(settings)
log = get_logger(__name__)

set_current_tenant(settings.default_tenant_id)
subject = scope_subject("inbox.message")  # -> "local.inbox.message"
log.info("ready", subject=subject)
```

Events (async):

```python
from epicurus_core import Event, EventBus


async def on_message(event: Event) -> None:
    print(event.json())


async with EventBus(settings.nats_url) as bus:
    await bus.subscribe("inbox.message", on_message, tenant_id="local")
    await bus.publish("inbox.message", {"text": "hi"}, tenant_id="local")
```
