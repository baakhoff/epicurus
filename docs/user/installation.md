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

## Bring up the data plane

The platform's backing services (Postgres, Valkey, NATS, Qdrant, OpenBao) come up
with one command:

```bash
docker compose -f infra/compose/docker-compose.yml up -d
# or, with go-task:
task infra-up
```

Check status:

```bash
docker compose -f infra/compose/docker-compose.yml ps
# or: task infra-ps
```

Postgres, Valkey, and OpenBao report a `healthy` status. NATS and Qdrant are
verified from the host:

```bash
curl localhost:8222/healthz        # NATS  -> {"status":"ok"}
curl localhost:6333/readyz         # Qdrant -> all shards are ready
curl localhost:8200/v1/sys/health  # OpenBao -> sealed:false (dev mode)
```

## Stop it

```bash
docker compose -f infra/compose/docker-compose.yml down       # keep data
docker compose -f infra/compose/docker-compose.yml down -v    # also remove volumes
# or: task infra-down  (append `-- -v` to drop volumes)
```

## Default ports

| Service | Port(s) |
| --- | --- |
| Postgres | 5432 |
| Valkey | 6379 |
| NATS | 4222 (client), 8222 (monitoring) |
| Qdrant | 6333 (HTTP), 6334 (gRPC) |
| OpenBao | 8200 |

Override any of them in a local `infra/compose/.env` — see
[Configuration](configuration.md).
