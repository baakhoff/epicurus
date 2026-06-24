# storage — file-tree index + object store

**`epicurus-storage`** v0.4.0 is a sidecar module that gives the agent access to a file
tree on disk — a **read-only index** it can list, search, and read — plus **app-managed
object storage** in MinIO for objects the platform itself creates. Host port **8083**.

The indexed tree is the **shared file space** (`/data`, `EPICURUS_FILES_ROOT`): storage reads
it read-only as the unified **Files** view, showing every file-owning module's folder —
`knowledge/` (knowledge bases) and `notes/` (the `.md` mirror of authored notes) — plus chat
uploads (#KB-refactor). The object store covers generated files, exports, and attachments.

v0.2.0 adds a **Files** left-nav page (the `browser` archetype, ADR-0018): browse the
indexed tree by directory, search by name, and download files — all core-rendered from
data the module supplies; no module markup runs in the shell.

v0.3.0 adds the **chat upload sink** (`POST /ingest`, ADR-0025): files attached in chat are
durably persisted to the object store and appear under an **`uploads/`** folder in the
Files page, downloadable like any other file.

v0.4.0 indexes the shared file space (so notes and knowledge bases show in Files) and adds a
**split-screen reader** — `GET /read` returns a UTF-8 text file's contents so the shell can
open a `.md` or text file in the core's right panel beside the list (#KB-refactor).

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
| `POST /ingest?filename=…&att_id=…` | **Chat upload sink (ADR-0025).** Body is the raw file bytes; `Content-Type` carries the media type. Stores the bytes in the object store under `uploads/<att_id>-<name>`, catalogues them (browsable + downloadable), and returns `{key, name, size}`. Called by the core's attachment-upload route. |
| `GET /pages/files?path=…&q=…` | `BrowserData`-shaped payload for the Files left-nav page (ADR-0018). `path` browses a directory (empty = root); `q` runs a search. Proxied by the core at `GET /platform/v1/modules/storage/pages/files`. |
| `GET /read?path=…` | **Split-screen reader (#KB-refactor).** Return a UTF-8 text file's contents → `{path, name, content}` — an `uploads/…` object decoded from MinIO, or a file from the read-only tree. **400** traversal, **404** missing, **413** larger than 256 KB, **415** binary / non-UTF-8. Proxied by the core at `GET /platform/v1/modules/storage/read`. |
| `GET /download?path=…` | Stream a file (binary-safe) — an `uploads/…` object from MinIO, or a file from the read-only tree. Path-traversal attempts → **HTTP 400**. Proxied by the core at `GET /platform/v1/modules/storage/download`. |
| `GET /health` · `GET /metrics` · `GET /manifest` | Ops + the module manifest. |

> **Path safety.** For tree files, both `storage_read` and `/download` resolve `(root / path)`
> and require it to stay within the configured root (`relative_to`), rejecting `..`, absolute
> paths, and symlink escapes. The tree is mounted **read-only**; uploaded objects live in the
> writable, tenant-scoped object bucket instead, and `/download` routes them there by their
> catalogued `source`.

### Events (NATS)

Emits **`<tenant>.storage.scan.completed`** after each full directory scan.

### Web UI (manifest)

Folder icon; a config form for the storage root; **Show status** and **Re-scan now**
actions — auto-rendered by the shell (ADR-0007).

### Left-nav page (ADR-0018)

The **Files** page (`archetype: browser`, `nav_order: 10`) appears in the left nav when
the storage module is reachable. It renders a two-pane tree/list + detail view:

- **List pane**: directories (with breadcrumb navigation) and files; search input when
  `search_enabled` is true.
- **Detail pane**: file name, size, and a **Download** button that fetches the file
  through the core proxy (`/platform/v1/modules/storage/download?path=…`).
- **Navigation**: clicking a directory drills in (the list refetches with `?path=…`);
  breadcrumbs let you navigate back up.
- **Split-screen reader (#KB-refactor)**: clicking a text/`.md` file opens it in the
  core's right panel (a `doc-reader` view, markdown rendered) **beside** the list, fetched
  through `GET /platform/v1/modules/storage/read?path=…`. A binary or oversized file falls
  back to download. This is how a knowledge-base note or a mirrored note is read in place.

The module supplies data only; the shell (`BrowserView`) owns all chrome and styling.

### The chat upload sink (ADR-0025)

When a user attaches a file in chat, the core's upload route keeps its core-side handle
**and** best-effort POSTs the bytes to this module's `POST /ingest`. The module:

1. **Stores the bytes** in the object bucket under `uploads/<att_id>-<name>` — the core
   attachment id makes the key unique, so two uploads of the same filename never collide.
2. **Catalogues** the upload in `storage_files` with `source="object"`, plus an `uploads`
   directory row, so it shows in the Files page (and `storage_search` finds it). The bytes
   live in MinIO; the index row is metadata pointing at them — exactly how scanned files
   point at bytes on disk.
3. **Serves it back**: `/download` sees the catalogued `source="object"` entry and streams
   the bytes from MinIO (with their stored content type), while filesystem paths still
   resolve against the read-only tree.

The read-only file tree (`scanner.py`) is untouched — a rescan's `purge_stale` only
removes `source="fs"` rows, so uploads survive every scan. Tenant scoping holds end to
end: the bytes land in the `{tenant}-storage` bucket and the catalogue rows are
tenant-scoped. The core treats persistence as **best-effort** — a down or absent storage
module never fails a chat upload.

## Configuration

`StorageSettings` extends [`CoreSettings`](../reference/config.md):

| Env var | Default | Meaning |
| --- | --- | --- |
| `STORAGE_ROOT` | `/data` | Absolute path (in-container) of the tree to index — the shared file space mount. |
| `DATABASE_URL` | `postgresql+asyncpg://…/epicurus` | The file index. |
| `MINIO_URL` | `http://minio:9000` | Object-store endpoint. |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | `epicurus` / `epicurus-dev` | Object-store creds (dev; OpenBao later). |

In the stack, the **shared file space** is bound to `/data` **read-only** via
`EPICURUS_FILES_ROOT` (the single env var that mounts the same `/data` tree across storage,
knowledge, and notes), which defaults to an **empty named volume** — nothing is exposed
until you point it at a real directory (never the host home dir). The same volume is mounted
**read-write** by knowledge (`/data/knowledge`) and notes (`/data/notes`), which own their
subfolders; storage only indexes and serves it. `EPICURUS_FILES_ROOT` **replaces** the old
per-module `STORAGE_HOST_ROOT`. See [Infrastructure](../infrastructure/index.md#shared-file-space).

## Data model

- **Postgres `storage_files`** — one row per indexed entry: `id`, `tenant`, `path`,
  `name`, `size`, `mtime`, `kind` (`file`/`dir`), `updated_at`, and `source`
  (`fs` = scanned, read-only · `object` = an `uploads/…` object in MinIO); unique on
  `(tenant, path)`. Tenant-scoped; a re-scan upserts and purges stale **`fs`** rows only.
  The `source` column is added in place at init on a pre-v0.3 deployment (no migration
  tool — the index uses `create_all`), backfilled to `fs`.
- **MinIO bucket `{tenant}-storage`** (`scope_bucket`) — app-managed objects, created
  lazily, one bucket per tenant. Chat uploads live here under the `uploads/` prefix; the
  `storage_object_*` tools store text objects in the same bucket.

## Dependencies

Postgres (the file index) · MinIO (objects) · NATS (the scan event) · the read-only
mounted directory tree. It uses **no AI** — pure filesystem + object I/O.

## Run & extend

```bash
EPICURUS_FILES_ROOT=/path/to/your/files docker compose up -d storage
```

Package `epicurus_storage`: `scanner.py` (walk + incremental upsert), `db.py`
(`storage_files` + queries + `source` column), `object_store.py` (MinIO via aioboto3 —
text **and** binary `put_bytes`/`get_object`), `service.py` (the MCP tools + manifest UI +
`build_page_data` + `ingest_object`/`load_object_download` + `load_text_file` for the inline
reader), `app.py` (lifespan + `/ingest` + `/download` + `/read` + `/pages/files`).
