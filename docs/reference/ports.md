# Host-port registry

Every service publishes its container's `8080` (or its native port) on a unique
**host** port, bound to `${BIND_ADDRESS:-127.0.0.1}` (loopback by default — the
[edge gateway](../infrastructure/index.md) is the intended front door). Two
fragments publishing the same host port is the classic wave-2 collision class
(#68): `compose config` still validates, but the stack fails to come up.

This page is the human-readable registry. The **machine source of truth is the
compose fragments themselves** — `scripts/new_module.py` and
`tests/test_compose_ports.py` scan `services/*/compose.yaml` and `infra/**` for
published ports, so the registry cannot silently drift from what actually binds.

## How a new module gets a port

`task new-module -- "My Module"` assigns the **lowest free port in the module
band** automatically and writes it into the fragment as
`${MY_MODULE_PORT:-<port>}` — overridable at runtime, unique by construction.
Pass `--port` to request a specific one; the scaffold refuses a port already in
use. Two guards back this up:

- **`tests/test_compose_ports.py`** (fast gate) fails if any two fragments
  publish the same host port — caught in seconds, no Docker.
- **`task smoke`** (`runtime-smoke`) repeats the check against the assembled
  `compose config` before boot.

## Module band — `8082–8099`

New modules are assigned here (echo sits just below at `8080`).

| Port | Service | Env override |
| --- | --- | --- |
| `8080` | echo | `ECHO_PORT` |
| `8081` | _(free)_ | — |
| `8082` | core-app | `CORE_PORT` |
| `8083` | storage | `STORAGE_PORT` |
| `8084` | web | `WEB_PORT` |
| `8085` | knowledge | `KNOWLEDGE_PORT` |
| `8086` | websearch | `WEBSEARCH_PORT` |
| `8087` | calendar | `CALENDAR_PORT` |
| `8088` | edge — HTTP entrypoint | `EDGE_HTTP_PORT` |
| `8089` | edge — Traefik dashboard | `EDGE_DASHBOARD_PORT` |
| `8090` | mail | `MAIL_PORT` |
| `8091` | tasks | `TASKS_PORT` |
| `8092–8099` | _(free — next module assigned here)_ | — |

## Data plane & ops

Listed so a new binding never clashes with these. Different bands, all
loopback-bound and individually overridable.

| Port | Service | Env override |
| --- | --- | --- |
| `4222` | NATS | `NATS_PORT` |
| `8222` | NATS monitoring | `NATS_MONITOR_PORT` |
| `5432` | Postgres | `POSTGRES_PORT` |
| `6379` | Valkey | `VALKEY_PORT` |
| `6333` | Qdrant — HTTP | `QDRANT_HTTP_PORT` |
| `6334` | Qdrant — gRPC | `QDRANT_GRPC_PORT` |
| `8200` | OpenBao | `OPENBAO_PORT` |
| `9000` | MinIO — API | `MINIO_API_PORT` |
| `9001` | MinIO — console | `MINIO_CONSOLE_PORT` |
| `9090` | Prometheus | `PROMETHEUS_PORT` |
| `9093` | Alertmanager | `ALERTMANAGER_PORT` |
| `3100` | Loki | `LOKI_PORT` |
| `4317` | OTel collector — gRPC | `OTLP_GRPC_PORT` |
| `4318` | OTel collector — HTTP | `OTLP_HTTP_PORT` |
| `3000` | Grafana | `GRAFANA_PORT` |

> Ollama (`11434`), SearXNG, `blackbox-exporter`, and `node-exporter` are
> internal-only — not published to the host. They take no host port.
