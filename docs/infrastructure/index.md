# Infrastructure

The backing services the core and modules rely on, plus the edge gateway and the
observability stack. They come up with the [compose stack](../user/installation.md) and
are **private by default** — every published port binds to `BIND_ADDRESS` (default
`127.0.0.1`), so nothing is reachable off the machine unless the operator opts in.

## Data plane

The stateful services every block builds on (`infra/compose/`):

| Service | Image | Host port(s) | Role |
| --- | --- | --- | --- |
| **Postgres** | `postgres:17` | 5432 | Relational store (tables per service: `agent_messages`, `storage_files`, `knowledge_notes`). |
| **Valkey** | `valkey/valkey:8` | 6379 | Cache / queues (Redis-compatible, SSPL-free — ADR-0002). |
| **NATS** | `nats:2.10` | 4222, 8222 | Event backbone (JetStream); 8222 = monitoring. |
| **Qdrant** | `qdrant/qdrant:v1.18.2` | 6333, 6334 | Vector DB — memory recall + knowledge RAG. |
| **OpenBao** | `openbao/openbao:2.2.0` | 8200 | Secrets — persistent file storage, auto-unseal sidecar. See [Secrets](secrets.md). |
| **MinIO** | `minio/minio` | 9000, 9001 | S3-compatible object store for app-managed objects. |

Dev credentials are intentionally weak and for a local, private box. OpenBao is the live
credential source — provider API keys set via the UI survive full stack restarts.
Details: [`infra/compose/README.md`](../../infra/compose/README.md).

## Edge gateway

**Traefik** routes to services by Docker label, with **no authentication baked in** and no
assumed ingress — the operator layers their own perimeter (Tailscale, a reverse proxy, an
auth proxy) in front (ADR-0008). Host ports **8088** (web entrypoint) and **8089**
(dashboard). Details: [`infra/edge/README.md`](../../infra/edge/README.md).

## Observability

**Grafana / Loki / Prometheus / Tempo** with an **Alloy** collector (OTel) give logs,
metrics, and traces for every container. Open Grafana at `http://localhost:3000`.
Alert rules for service health, OpenBao sealed, and disk usage are pre-configured and
evaluated by Prometheus; Alertmanager routes notifications (edit
`infra/observability/alertmanager/alertmanager.yml` to add a receiver).
Details: [`infra/observability/README.md`](../../infra/observability/README.md).

## Ollama (local LLM runtime)

Runs **as a container** (ADR-0011), CPU by default and GPU opt-in via an overlay
(`infra/ollama/gpu.yaml`). Internal-only — the core is the front door for model access — and
**models are pulled and managed by the core at runtime**, never baked into an image.
Details: [`infra/ollama/README.md`](../../infra/ollama/README.md).

## How it's assembled

The root `compose.yaml` `include`s the infra fragment and each module fragment (ADR-0006):

```bash
docker compose up -d                                        # the whole stack
docker compose -f infra/compose/docker-compose.yml up -d    # data plane only
```

See the [Architecture](../developer/architecture.md) guide for how the pieces fit.

## Operations

- [Auto-deploy (CD)](auto-deploy.md) — how a released tag rolls out to the box
  automatically (scheduled reconcile script or Watchtower), and how to roll back.
- [Startup and recovery](startup-and-recovery.md) — configure Docker Desktop
  launch-on-login (Windows) and recovery procedures for common failure modes.
- [Backup and restore](backup-and-restore.md) — snapshot volumes, store the
  unseal key off-box, and run a verified restore.
