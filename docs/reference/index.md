# API Reference

This reference documents the public API of **`epicurus-core`** — the library every
service and module is built on. It's organized by module: each page lists the
classes and functions, what they do, their parameters, and how they fit together.

Everything below is importable from the top level, e.g.
`from epicurus_core import CoreSettings, EventBus, EpicurusModule`.

## How the pieces fit together

A typical service wires the building blocks in this order:

1. **[`config`](config.md)** — load `CoreSettings` from the environment.
2. **[`logging`](logging.md)** — `configure_logging(settings)` once at startup;
   `get_logger()` anywhere after.
3. **[`tenancy`](tenancy.md)** — bind the current tenant; every resource name is
   scoped by it.
4. Use the capability clients as needed:
   - **[`events`](events.md)** — `EventBus` for NATS publish / subscribe / request.
   - **[`secrets`](secrets.md)** — `SecretStore` for tenant-scoped secrets in OpenBao.
   - **[`modules`](modules.md)** — `EpicurusModule` to expose MCP tools + a manifest.
5. **[`observability`](observability.md)** — mount `/health` + `/metrics`, and
   `setup_tracing(app, settings)` for optional OpenTelemetry traces to Tempo.

```python
from epicurus_core import CoreSettings, configure_logging, get_logger, set_current_tenant

settings = CoreSettings()
configure_logging(settings)
log = get_logger(__name__)
set_current_tenant(settings.default_tenant_id)
log.info("service starting", service=settings.service_name)
```

## Modules at a glance

| Module | Provides |
| --- | --- |
| [`config`](config.md) | `CoreSettings`, `Environment`, `LogLevel` |
| [`logging`](logging.md) | `configure_logging`, `get_logger` |
| [`tenancy`](tenancy.md) | tenant validation, `scope_*` helpers, current-tenant context |
| [`events`](events.md) | `EventBus`, `Event` (+ `Payload`, `EventHandler`, `Replier`) |
| [`messaging`](messaging.md) | `InboundMessage`, `OutboundMessage`, `MessageAttachment`, `MESSAGING_INBOUND/OUTBOUND`, `session_id_for` — the chat-bridge inbox contract (ADR-0058) |
| [`modules`](modules.md) | `EpicurusModule`, `ModuleManifest`, `ToolSpec`, `EventSpec`, `CONTRACT_VERSION` |
| [`observability`](observability.md) | `add_ops_routes`, `create_ops_router`, `HealthResponse` |
| [`tracing`](observability.md#tracing-57-adr-0068) | `setup_tracing`, `get_tracer` — optional OpenTelemetry traces to Tempo (#57) |
| [`secrets`](secrets.md) | `SecretStore`, `SecretError` |
| [`platform-client`](platform-client.md) | `PlatformClient`, `PlatformMessage` — a module's typed access to core inference |
| [`files`](files.md) | `FileStore`, `FileEntry`, `build_file_store` — the core-owned, swappable per-tenant file space (ADR-0052) |

The module↔core **wire contract** (the HTTP endpoints behind `PlatformClient`) is
documented in [platform-api](platform-api.md). The running services that consume all of
this are under [Services & Modules](../services/index.md). The host ports each one
publishes — and how a new module gets a collision-free one — are in the
[host-port registry](ports.md).
