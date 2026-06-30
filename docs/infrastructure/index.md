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
| **Qdrant** | `qdrant/qdrant:${QDRANT_TAG}` | 6333, 6334 | Vector DB — memory recall + knowledge RAG. Upgrade-safe via the `qdrant-init` guard — see [Qdrant](qdrant.md). |
| **OpenBao** | `openbao/openbao:2.2.0` | 8200 | Secrets — persistent file storage, auto-unseal sidecar. See [Secrets](secrets.md). |
| **MinIO** | `minio/minio` | 9000, 9001 | S3-compatible object store for app-managed objects. |

Dev credentials are intentionally weak and for a local, private box. OpenBao is the live
credential source — provider API keys set via the UI survive full stack restarts.
Details: [`infra/compose/README.md`](../../infra/compose/README.md).

### Shared file space

The file-owning modules share **one** file tree — the **shared file space** (#KB-refactor).
It is a single volume mounted at `/data` in each of them, set by one env var
**`EPICURUS_FILES_ROOT`** (default an empty named volume, `epicurus-files`; point it at a host
directory to expose real files — never the host home dir). The on-disk tree is
**tenant-scoped** (constraint #1): every module inserts a `<tenant>/` segment so the layout is
`/data/<tenant>/…`, where `<tenant>` is `DEFAULT_TENANT_ID` (default `local`). The mount stays
`/data`; only the in-container path carries the segment. The **core** mounts the volume and the
file-owning modules each own a subtree:

- **core** mounts it and owns the **file index** over the tenant subtree `/data/<tenant>` — it
  scans + watches the tree and serves the unified **Files** view (browser / read / download),
  merging in the storage module's objects (ADR-0063). The **storage** module **no longer mounts
  `/data`**; it reads the file space through the core file API.
- **knowledge** mounts it **read-only** and owns `/data/<tenant>/knowledge` (each top-level
  folder is a knowledge base / project). It **reads** + indexes the tree on this mount but
  **writes** through the core file API (`PlatformClient.files_*`, core path `knowledge/<rel>`),
  so the core performs the on-disk write (#356, ADR-0064).
- **notes** mounts it **read-write** and owns `/data/<tenant>/notes` (the read-only `.md` mirror
  of authored notes; Postgres stays the source of truth).

`EPICURUS_FILES_ROOT` **replaces** the old per-module `KNOWLEDGE_HOST_VAULT` and
`STORAGE_HOST_ROOT`; existing deployments move old vault contents into
`<files-root>/<tenant>/knowledge/<project>/` (`<tenant>` = `DEFAULT_TENANT_ID`, default `local`).

A one-shot **`files-init`** container prepares the tree before any module starts (it is a
`depends_on: service_completed_successfully` of storage / knowledge / notes, mirroring
`qdrant-init` and the OpenBao chown). A fresh named volume is created **root-owned**, but the
modules run as uid 10001 — so without this they would hit `PermissionError` (HTTP 500)
creating folders or saving documents. `files-init` creates `/data/<tenant>/knowledge` and
`/data/<tenant>/notes` and chowns **only those** to uid 10001, leaving the rest of a
bind-mounted tree (e.g. an operator's Obsidian vault) untouched.

## Edge gateway

**Traefik** routes to services by Docker label, with **no authentication baked in** and no
assumed ingress — the operator layers their own perimeter (Tailscale, a reverse proxy, an
auth proxy) in front (ADR-0008). Host ports **8088** (web entrypoint) and **8089**
(dashboard). Details: [`infra/edge/README.md`](../../infra/edge/README.md).

## Observability (opt-in)

**Grafana / Loki / Prometheus / Tempo** with an **Alloy** collector (OTel) give logs,
metrics, and traces for every container — but the whole stack is **opt-in**, gated behind
the `observability` compose profile. A plain `docker compose up` runs without it; bring it
up with `docker compose --profile observability up -d` and open Grafana at
`http://localhost:3000`. Nothing in epicurus depends on it at runtime: every service exposes
`/metrics` and `/health` regardless, so you can also point your **own** Prometheus/Grafana
(or any monitoring you prefer) at those endpoints and never enable this stack at all.
Alert rules for service health, OpenBao sealed, and disk usage are pre-configured and
evaluated by Prometheus; Alertmanager routes notifications (edit
`infra/observability/alertmanager/alertmanager.yml` to add a receiver).
Details: [`infra/observability/README.md`](../../infra/observability/README.md).

## Ollama (local LLM runtime)

Runs **as a container** (ADR-0011), CPU by default and GPU opt-in via an overlay
(`infra/ollama/gpu.yaml`). Internal-only — the core is the front door for model access — and
**models are pulled and managed by the core at runtime**, never baked into an image.
Details: [`infra/ollama/README.md`](../../infra/ollama/README.md).

Ollama and core-app share a small named volume, **`ollama-runtime`**, mounted at
`/etc/epicurus`: the core writes `ollama.env` there to apply the operator's KV-cache choice
(#307), and Ollama mounts it **read-only** and sources it on (re)start. A one-shot
**`ollama-init`** container prepares that volume before Ollama starts (a
`depends_on: service_completed_successfully`, mirroring `qdrant-init`). A fresh named volume is
created **root-owned**, but core-app runs as uid 10001 — so without this its env-file write would
hit `PermissionError` and the choice would save but never apply (#392). `ollama-init` simply
`chown`s the volume root to uid 10001 (the env file lives directly at the root). It is **ordering
only**: the core's write is lazy — it happens when the operator changes the KV-cache type, long
after boot — so there is no startup race regardless.

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
- [Qdrant (vector store + upgrades)](qdrant.md) — the upgrade auto-recovery guard
  (`qdrant-init`), the healthcheck, and the qdrant version policy.
