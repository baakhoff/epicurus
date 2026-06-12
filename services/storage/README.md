# epicurus-storage

Read-only file-tree index module for epicurus. Indexes a configurable root
directory into Postgres and exposes browse, search, and download capabilities
to the agent and the web shell.

## What it does

- **Indexes** a mounted directory tree into Postgres on startup; a re-scan
  picks up additions and deletions. Each row is scoped by `tenant_id`.
- **Browse** — MCP tool `storage_browse(path)` lists the direct children of a
  directory (empty string = root).
- **Search** — MCP tool `storage_search(query, limit)` returns files and
  directories whose name or path contains the query string.
- **Rescan** — MCP tool `storage_rescan()` triggers a fresh walk on demand.
- **Download** — `GET /download?path=<relative-path>` streams a file from the
  root. Path-traversal attempts are rejected with HTTP 400.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `STORAGE_ROOT` | `/data` | Absolute path (inside the container) of the root directory to index. Mount your HDD here. |
| `DATABASE_URL` | `postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus` | Async Postgres DSN. |
| `NATS_URL` | `nats://localhost:4222` | NATS server (inherited from CoreSettings). |
| `DEFAULT_TENANT_ID` | `local` | Tenant scope for all indexed rows. |
| `STORAGE_HOST_ROOT` | _(empty named volume)_ | Host directory to index, bound read-only to `/data`. Unset → an empty named volume (nothing on the host is exposed); set it to your data directory to index real files. |

## Running locally

```bash
# From the repo root:
STORAGE_HOST_ROOT=/mnt/data docker compose up storage
```

The service appears in the web shell's Modules tab once it's healthy.
