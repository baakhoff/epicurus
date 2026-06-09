# epicurus-core

Shared contract and runtime used by every epicurus service.

## Available now

- **`config`** — `CoreSettings` (pydantic-settings): env-driven, non-secret
  configuration shared by every service. Subclass to add service fields.
- **`logging`** — `configure_logging` / `get_logger`: structlog, console in local
  dev and JSON otherwise, with contextvar-based correlation.
- **`tenancy`** — the dual-track primitive. Scopes every NATS subject, Qdrant
  collection, OpenBao secret path, and object bucket by tenant, plus a
  contextvar-bound "current tenant" (see [AGENTS.md](../../AGENTS.md)
  non-negotiables and [docs/DUAL-TRACK.md](../../docs/DUAL-TRACK.md)).
- **`observability`** — `add_ops_routes` / `create_ops_router`: the shared
  `GET /health` + `GET /metrics` (Prometheus) surface.

## Pending (follow-up changes)

- NATS client (events backbone)
- MCP base classes (the module tool contract)
- OpenBao client (secret access)
- OpenTelemetry tracing helpers

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
