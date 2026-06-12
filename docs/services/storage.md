# storage — file-tree index + object store

**`epicurus-storage`** is a sidecar module that gives the agent access to a file tree on
disk — a **read-only index** it can list, search, and read — plus **app-managed object
storage** in MinIO for objects the platform itself creates. Host port **8083**.

The read-only tree covers the operator's existing files (e.g. an HDD); the object store
covers generated files, exports, and attachments.

## The contract it exposes

### MCP tools (agent-facing)

| Tool | Purpose |
| --- | --- |
| `storage_list(path="")` | List the direct children of `path` (dirs before files). |
| `storage_search(query, limit=50)` | Case-insensitive name/path search (max 200). |
| `storage_read(path)` | Return a text file's contents. Rejects files > **256 KB** and non-UTF-8 (binary) with an explanatory message. |
| `storage_status()` | Configured root + indexed file/dir counts. |
| `storage_rescan()` | Re-walk the tree and refresh the index. |
| `storage_object_put(key, content)` | Store a text object under `key` (tenant bucket). |
| `storage_object_get(key)` | Retrieve a stored object (or `null`). |

### HTTP

| Method · Path | Purpose |
| --- | --- |
| `GET /download?path=…` | Stream a file (binary-safe). Path-traversal attempts → **HTTP 400**. |
| `GET /health` · `GET /metrics` · `GET /manifest` | Ops + the module manifest. |

> **Path safety.** Both `storage_read` and `/download` resolve `(root / path)` and require
> it to stay within the configured root (`relative_to`), rejecting `..`, absolute paths,
> and symlink escapes. The tree is mounted **read-only**.

### Events (NATS)

Emits **`<tenant>.storage.scan.completed`** after each full directory scan.

### Web UI (manifest)

Folder icon; a config form for the storage root; **Show status** and **Re-scan now**
actions — auto-rendered by the shell (ADR-0007).

## Configuration

`StorageSettings` extends [`CoreSettings`](../reference/config.md):

| Env var | Default | Meaning |
| --- | --- | --- |
| `STORAGE_ROOT` | `/data` | Absolute path (in-container) of the tree to index. |
| `DATABASE_URL` | `postgresql+asyncpg://…/epicurus` | The file index. |
| `MINIO_URL` | `http://minio:9000` | Object-store endpoint. |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | `epicurus` / `epicurus-dev` | Object-store creds (dev; OpenBao later). |

In the stack, the host directory is bound to `/data` **read-only** via
`STORAGE_HOST_ROOT`, which defaults to an **empty named volume** — nothing is exposed
until you point it at a real directory (never the host home dir).

## Data model

- **Postgres `storage_files`** — one row per indexed entry: `id`, `tenant`, `path`,
  `name`, `size`, `mtime`, `kind` (`file`/`dir`), `updated_at`; unique on
  `(tenant, path)`. Tenant-scoped; a re-scan upserts and purges stale rows.
- **MinIO bucket `{tenant}-storage`** (`scope_bucket`) — app-managed objects, created
  lazily, one bucket per tenant.

## Dependencies

Postgres (the file index) · MinIO (objects) · NATS (the scan event) · the read-only
mounted directory tree. It uses **no AI** — pure filesystem + object I/O.

## Run & extend

```bash
STORAGE_HOST_ROOT=/path/to/your/files docker compose up -d storage
```

Package `epicurus_storage`: `scanner.py` (walk + incremental upsert), `db.py`
(`storage_files` + queries), `object_store.py` (MinIO via aioboto3), `service.py` (the MCP
tools + manifest UI), `app.py` (lifespan + `/download`).
