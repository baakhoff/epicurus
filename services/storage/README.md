# epicurus-storage

File-tree index and object-storage module for epicurus. Indexes a configurable
root directory into Postgres, exposes agent file-access tools (list, search,
read), and provides app-managed object storage via MinIO.

## What it does

### File-tree tools (read-only, indexed from HDD)

| MCP tool | Description |
|---|---|
| `storage_list(path)` | List direct children of a directory (`""` = root). |
| `storage_search(query, limit)` | Case-insensitive name/path search; up to 200 results. |
| `storage_read(path)` | Return text file contents. Files > 256 KB or binary are rejected. |
| `storage_status()` | Show configured root and indexed file/directory counts. |
| `storage_rescan()` | Re-walk the root directory and refresh the index. |

**Download** — `GET /download?path=<relative-path>` streams any file (including
binary and large). Path-traversal attempts are rejected with HTTP 400.

### Object-store tools (read/write, backed by MinIO)

| MCP tool | Description |
|---|---|
| `storage_object_put(key, content)` | Store a UTF-8 text object under `key`. |
| `storage_object_get(key)` | Retrieve text at `key`; returns `null` if missing. |

Objects are scoped to a per-tenant bucket (`{tenant}-storage`), created lazily
on first write.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `STORAGE_ROOT` | `/data` | In-container base of the indexed tree; storage serves the tenant subtree `STORAGE_ROOT/<tenant>` (tenant-scoped, constraint #1; `<tenant>` = `DEFAULT_TENANT_ID`). |
| `DATABASE_URL` | `postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus` | Async Postgres DSN for the file index. |
| `NATS_URL` | `nats://localhost:4222` | NATS server (inherited from CoreSettings). |
| `DEFAULT_TENANT_ID` | `local` | Tenant scope for all rows and object buckets. |
| `STORAGE_HOST_ROOT` | _(empty named volume)_ | Host directory bound read-only to `/data`. Unset = nothing indexed; set to your data directory to index real files. |
| `MINIO_URL` | `http://minio:9000` | MinIO (S3-compatible) API endpoint. |
| `MINIO_ACCESS_KEY` | `epicurus` | MinIO access key. |
| `MINIO_SECRET_KEY` | `epicurus-dev` | MinIO secret key. |

## Running locally

```bash
# From the repo root:
STORAGE_HOST_ROOT=/mnt/data docker compose up storage
```

The service appears in the web shell's Modules tab once it's healthy.
