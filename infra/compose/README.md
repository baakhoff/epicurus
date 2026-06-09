# Data-plane compose

The stateful backing services every epicurus module builds on. Application
services and the edge (a gateway and private ingress) are layered on separately.

## Services

| Service | Image | Host port(s) | Purpose |
| --- | --- | --- | --- |
| postgres | `postgres:17` | 5432 | Relational store (schema-per-service) |
| valkey | `valkey/valkey:8` | 6379 | Cache / queues / rate-limit (Redis-compatible, BSD) |
| nats | `nats:2.10` | 4222, 8222 | Event backbone (JetStream); 8222 = monitoring |
| qdrant | `qdrant/qdrant:v1.12.4` | 6333, 6334 | Vector DB (RAG + memory) |
| openbao | `openbao/openbao:2.2.0` | 8200 | Secrets (dev mode here; real mode later) |

## Bring up

```bash
docker compose -f infra/compose/docker-compose.yml up -d   # or: task infra-up
docker compose -f infra/compose/docker-compose.yml ps      # or: task infra-ps
docker compose -f infra/compose/docker-compose.yml down     # add -v to drop volumes
```

Verify (host): `curl localhost:8222/healthz` (NATS), `curl localhost:6333/readyz`
(Qdrant), `curl localhost:8200/v1/sys/health` (OpenBao). Postgres and Valkey have
in-container healthchecks (`docker compose ps` shows `healthy`).

## Configuration & secrets

Local-dev defaults are inline in the compose file. Override them in a local
`infra/compose/.env` (gitignored) — copy from `.env.example`. The dev credentials
(Postgres password, OpenBao root token) are **for a local, private box only**.
In staging/production these come from OpenBao and a non-dev OpenBao
deployment; nothing sensitive is committed.

## Not here yet (follow-ups)

- **Edge** — gateway + private ingress (paired, since the gateway has
  nothing to route until app services exist).
- **Observability** — Grafana / Loki / Prometheus / Tempo + OTel collector, as a
  separate stack.
