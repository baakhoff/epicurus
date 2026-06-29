# epicurus-storage

Agent file-access tools over the **core-owned file space** plus app-managed object
storage for epicurus. After the file-space migration (ADR-0063) the **core** owns the
unified file space and serves the Files browser UI; this module is a consumer of it
(via the platform API) and the owner of the MinIO object store. It exposes agent file
tools (list, search, read) that read the core file space, app-managed object storage,
the durable chat-upload sink, and the object surface the core's Files view proxies.

## What it does

### File tools (over the core-owned file space + the module's objects)

These read the core file space through the platform API (no `/data` mount) and merge in
the module's own catalogued objects. Top-level subtrees listed in `AGENT_HIDDEN_PREFIXES`
(default `notes`) are hidden from the agent — the operator still browses them in the core
Files page.

| MCP tool | Description |
|---|---|
| `storage_list(path)` | List direct children of a directory (`""` = root): file-space children + catalogued objects, dirs first by name. |
| `storage_search(query, limit)` | Case-insensitive name/path search across the file space + objects; up to 200 results. |
| `storage_read(path)` | Return text file contents (object store first, then the file space). Files > 256 KB or binary are rejected. |
| `storage_status()` | Object-store entry counts (`object_files`, `object_dirs`). |

### Object-store tools (read/write, backed by MinIO)

| MCP tool | Description |
|---|---|
| `storage_object_put(key, content)` | Store a UTF-8 text object under `key`; catalogued so it shows in the Files page. |
| `storage_object_get(key)` | Retrieve text at `key`; returns `null` if missing. |

Objects are scoped to a per-tenant bucket (`{tenant}-storage`), created lazily on first
write. A nested key (e.g. `reports/q2.md`) creates the enclosing folders.

### HTTP object surface (the core's Files view proxies these)

| Route | Description |
|---|---|
| `POST /ingest?filename=&att_id=` | Chat-upload sink (ADR-0025): persist raw bytes to the object store and catalogue them. |
| `GET /objects?path=&q=` | Browse (empty `q`) or search object-store entries → `{entries:[{path,name,size,mtime,kind}]}`. |
| `GET /objects/read?path=` | Return a catalogued object's text → `{path,name,content}` (404/413/415 on error). |
| `GET /download?path=` | Stream a catalogued **object** from MinIO (404 if the path is not a catalogued object — no filesystem fallback). |
| `POST /objects/move` | Move/rename a writable object-store entry (`{from_path,to_path}` → `{path}`, #381 / #391). |

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PLATFORM_URL` | `http://localhost:8080` | Core base URL — the agent file tools read the core-owned file space through the platform API (compose sets `http://core-app:8080`). |
| `AGENT_HIDDEN_PREFIXES` | `notes` | Comma-separated top-level subtrees hidden from the agent's file tools (still browsable by the operator in the core Files page). |
| `DATABASE_URL` | `postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus` | Async Postgres DSN for the object index. |
| `NATS_URL` | `nats://localhost:4222` | NATS server (inherited from CoreSettings). |
| `DEFAULT_TENANT_ID` | `local` | Tenant scope for all rows and object buckets. |
| `MINIO_URL` | `http://minio:9000` | MinIO (S3-compatible) API endpoint. |
| `MINIO_ACCESS_KEY` | `epicurus` | MinIO access key. |
| `MINIO_SECRET_KEY` | `epicurus-dev` | MinIO secret key. |

## Running locally

```bash
# From the repo root:
docker compose up storage
```

The service appears in the web shell's Modules tab once it's healthy.
