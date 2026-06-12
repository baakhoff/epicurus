# Installation

## Prerequisites

- **Docker** (with Docker Compose v2) — on Windows, use Docker Desktop with the
  WSL2 backend.
- **[uv](https://docs.astral.sh/uv/)** — only needed if you want to run the
  Python tooling or tests; not required just to run the stack.
- Optionally **[go-task](https://taskfile.dev)** for the `task` shortcuts.

## Get the code

```bash
git clone https://github.com/baakhoff/epicurus.git
cd epicurus
```

## Bring up the stack

The whole stack — the backing services plus the modules — comes up with one
command (the top-level `compose.yaml` assembles them from per-module fragments):

```bash
docker compose up -d
# or, with go-task:
task up
```

Check status with `docker compose ps`. To run *only* the data-plane backing
services (Postgres, Valkey, NATS, Qdrant, OpenBao, MinIO) without any modules:

```bash
docker compose -f infra/compose/docker-compose.yml up -d   # or: task infra-up
```

Postgres, Valkey, OpenBao, and MinIO report a `healthy` status. NATS, Qdrant,
and MinIO are also verified from the host:

```bash
curl localhost:8222/healthz        # NATS  -> {"status":"ok"}
curl localhost:6333/readyz         # Qdrant -> all shards are ready
curl localhost:8200/v1/sys/health  # OpenBao -> sealed:false (dev mode)
curl localhost:9000/minio/health/live  # MinIO -> HTTP 200
curl localhost:8080/health         # echo module -> {"status":"ok","service":"echo",...}
```

## Stop it

```bash
docker compose down       # keep data
docker compose down -v    # also remove volumes
# or: task down  (append `-- -v` to drop volumes)
```

## Default ports

All host ports bind to **127.0.0.1** by default — nothing is reachable from
another machine until you opt in. To expose the stack more widely, set
`BIND_ADDRESS` (e.g. `0.0.0.0`) in your `.env` and put your own perimeter (VPN,
reverse proxy, auth proxy) in front — see [Configuration](configuration.md).

| Service | Port(s) |
| --- | --- |
| Postgres | 5432 |
| Valkey | 6379 |
| NATS | 4222 (client), 8222 (monitoring) |
| Qdrant | 6333 (HTTP), 6334 (gRPC) |
| OpenBao | 8200 |
| MinIO | 9000 (S3 API), 9001 (console) |
| Gateway (Traefik) | 8088 (web), 8089 (dashboard) |
| core-app (runtime) | 8082 |
| web (UI shell) | 8084 |
| echo (module) | 8080 |
| Grafana | 3000 |
| Prometheus | 9090 |
| Loki | 3100 |
| Tempo (OTLP) | 4317 (gRPC), 4318 (HTTP) |

Once the stack is up, open the **web UI** at <http://localhost:8088/> — chat with
the agent, manage models and provider keys, configure modules, and toggle power.
**Grafana** at <http://localhost:3000> has logs, metrics, and traces. To change any
host port, set it in your root `.env` (the full stack reads it) — see
[Configuration](configuration.md).
