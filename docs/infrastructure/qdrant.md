# Qdrant — vector store + upgrade auto-recovery

Qdrant is the data-plane vector database. It holds **derived** data only — every
collection can be rebuilt from a source of truth:

- `<tenant>__knowledge` / `<tenant>__docs` — re-embedded by the knowledge module from
  the vault, the bundled platform docs, and module-contributed docs.
- `<tenant>__memory` — re-embedded by core-app from the Postgres message store.

Because the vectors are reproducible, the safe response to an incompatible on-disk
store is to **reset and re-index**, not to block the deploy. This page documents the
version guard that does exactly that (#229; rationale in ADR-0032).

## The problem it solves

Qdrant does **not** migrate its on-disk segment format across some server upgrades.
The v1.12 → v1.18 jump (#199) is one such break: a v1.18 server started against a
v1.12 data directory panics on boot —

```
Panic ... Failed to deserialize .../segment.json: unknown variant `on_disk`,
expected `mmap` or `in_ram_mmap`
```

— and `restart: unless-stopped` turns that into a **crash loop**. A crash-looping
qdrant drops out of Docker DNS, so every dependent (knowledge, core-app memory) then
fails with `[Errno -2] Name or service not known`. Fresh installs are fine; only
**upgraded** deploys break, so CI never sees it.

## The guard (`qdrant-init`)

A one-shot `qdrant-init` service runs **before** qdrant and reconciles the data
volume against the target server version:

1. It reads a marker file, `/qdrant/storage/.epicurus_qdrant_version`, written into
   the `qdrant-data` volume.
2. If the marker **matches** `QDRANT_TAG` → no action.
3. If the marker **differs** → the on-disk format may be incompatible, so it **wipes**
   the store contents and rewrites the marker. qdrant then starts clean and the
   derived data re-indexes.
4. If the marker is **absent** but the store is non-empty (a volume that predates this
   guard) → it **stamps** the current `QDRANT_TAG` without wiping, trusting the
   running version. (The one historical break that predates the marker was remediated
   manually; every future bump carries a marker and auto-recovers.)

qdrant depends on it with `condition: service_completed_successfully`, so the guard
always runs first.

```yaml
qdrant-init:
  image: alpine:3.21
  environment:
    QDRANT_TAG: ${QDRANT_TAG:-v1.18.2}
  volumes:
    - qdrant-data:/qdrant/storage
  # compares the marker to QDRANT_TAG; wipes the store on a mismatch
qdrant:
  image: qdrant/qdrant:${QDRANT_TAG:-v1.18.2}
  depends_on:
    qdrant-init:
      condition: service_completed_successfully
```

## Healthcheck

The qdrant image ships no `curl`/`wget`, so the healthcheck probes the HTTP listener
through `/proc`: it greps `/proc/net/tcp[6]` for a socket **listening** on port
`6333` (`0x18BD`, state `0A`). A crash-looping qdrant never binds the port, so it
reports **unhealthy** — surfacing the crash in `docker compose ps` instead of hiding
it behind a restart counter. Dependents wait on `condition: service_healthy`.

## The client side — self-heal re-index

Wiping the volume is only half the recovery: the knowledge module's Postgres ledgers
(`knowledge_notes`, `knowledge_doc_index`, `knowledge_module_docs`) still record every
file as indexed, so a plain incremental run would skip everything and leave the fresh
collection empty. The knowledge indexers therefore **reconcile** on each index pass:
if a collection is missing but its ledger is non-empty, the ledger is cleared so the
run re-embeds from scratch. The reconcile pre-pass runs for *all* sources before any
of them recreates the shared `<tenant>__docs` collection. See
[knowledge](../services/knowledge.md) (`runner.IndexRunner`, `KnowledgeIndexer.reconcile`).

core-app's `<tenant>__memory` re-populates lazily as new messages are embedded; a
full backfill of historical messages is a possible follow-up, not part of this guard.

## Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `QDRANT_TAG` | `v1.18.2` | Single source of truth for the qdrant **server image tag** *and* the init marker. Bump this (not the hard-coded image) to upgrade. |
| `QDRANT_HTTP_PORT` | `6333` | Published HTTP port (bound to `BIND_ADDRESS`). |
| `QDRANT_GRPC_PORT` | `6334` | Published gRPC port. |

## Version policy

- The qdrant **server** minor must stay within 1 of the `qdrant-client` pin
  (currently 1.18) or the client refuses to connect. Bump the server image and the
  Python client **together**.
- `QDRANT_TAG` drives both the server image and the init marker, so a version change
  is a single edit and the guard reacts to it automatically.
- Treat every qdrant **minor** bump as potentially format-breaking: the guard will
  reset the store and the derived data re-indexes in the background (#230). This is
  intentional — vectors are reproducible; correctness and a non-stranded deploy beat
  preserving a re-derivable index.
- Pin a tag you have actually pulled. A bad tag passes `compose config` but fails on
  `up`; `task smoke` boots the stack and catches it.

## Upgrading qdrant — checklist

1. Bump the `qdrant-client` pin in `services/*/pyproject.toml` (and `core-app`) and
   `uv lock`.
2. Bump `QDRANT_TAG` in `.env` (or the default in `infra/compose/docker-compose.yml`).
3. `docker compose up -d` — `qdrant-init` resets the store if the format changed,
   qdrant comes up healthy, and knowledge re-indexes in the background.
4. Watch `GET /platform/v1/modules/knowledge/status` until `index_phase` is `ready`
   and `doc_count` / `note_count` recover.

## Dependencies

The `qdrant-data` named volume (storage). No external dependencies; the guard uses a
stock `alpine` image to manipulate the volume before the server starts.
