# storage — file-tree index + object store

**`epicurus-storage`** v0.5.1 is a sidecar module that gives the agent access to a file
tree on disk — a **read-only index** it can list, search, and read — plus **app-managed
object storage** in MinIO for objects the platform itself creates. Host port **8083**.

The indexed tree is the **shared file space** (`/data`, `EPICURUS_FILES_ROOT`): storage reads
the **tenant subtree** `/data/<tenant>` read-only as the unified **Files** view (tenant-scoped,
constraint #1; `<tenant>` = `DEFAULT_TENANT_ID`, default `local`), showing every file-owning
module's folder — `knowledge/` (knowledge bases) and `notes/` (the `.md` mirror of authored
notes) — plus chat uploads (#KB-refactor). The object store covers generated files, exports,
and attachments.

**Private subtrees are hidden from the agent (#KB-refactor).** Some folders are
operator-only: the `notes/` mirror holds **private** note bodies the agent must never read.
The agent's **file tools** (`storage_list`/`storage_search`/`storage_read`) therefore exclude
the subtrees named in `STORAGE_AGENT_HIDDEN_PREFIXES` (default `notes`), while the
**operator-facing** surfaces — the Files page, `GET /read`, `GET /download` — show and read
everything. So a note stays browsable and readable in the Files UI but invisible to the agent
through the file tools (its content reaches the agent only when the user attaches it; see
[notes](notes.md)).

v0.2.0 adds a **Files** left-nav page (the `browser` archetype, ADR-0018): browse the
indexed tree by directory, search by name, and download files — all core-rendered from
data the module supplies; no module markup runs in the shell.

v0.3.0 adds the **chat upload sink** (`POST /ingest`, ADR-0025): files attached in chat are
durably persisted to the object store and appear under an **`uploads/`** folder in the
Files page, downloadable like any other file.

v0.4.0 indexes the shared file space (so notes and knowledge bases show in Files) and adds a
**split-screen reader** — `GET /read` returns a UTF-8 text file's contents so the shell can
open a `.md` or text file in the core's right panel beside the list (#KB-refactor).

v0.5.0 **hides private subtrees from the agent's file tools** (#KB-refactor): the `notes/`
mirror (private note bodies) is excluded from `storage_list`/`storage_search`/`storage_read`,
configurable via `STORAGE_AGENT_HIDDEN_PREFIXES` (default `notes`). The operator's Files page,
`/read`, and `/download` are unchanged — notes stay browsable and readable for the human.

v0.5.1 **makes agent-written objects appear in Files** (#347): `storage_object_put` now
catalogues the object it stores (a `source="object"` index row plus any ancestor folder rows),
so a file the agent saves shows up in the Files page and is searchable, readable, and
downloadable — exactly like a chat upload. Previously the bytes landed in MinIO but no index
row existed, so the browser (which lists the index, not the bucket) never showed them. `/read`,
`/download`, and `storage_read` now resolve **any** catalogued object by its `source`, no longer
only those under the `uploads/` prefix.

## The contract it exposes

### MCP tools (agent-facing)

> The three **file-tree** tools below never see the subtrees in
> `STORAGE_AGENT_HIDDEN_PREFIXES` (default `notes`): `storage_list`/`storage_search` filter
> them out and `storage_read` refuses them with `Error: not available` (#KB-refactor). The
> operator-facing Files page / `/read` / `/download` are unaffected.

| Tool | Purpose |
| --- | --- |
| `storage_list(path="")` | List the direct children of `path` (dirs before files). A hidden subtree (e.g. `notes/`) yields nothing. |
| `storage_search(query, limit=50)` | Case-insensitive name/path search (max 200). Hits under a hidden subtree are filtered out. |
| `storage_read(path)` | Return a text file's contents — a tree file **or** an agent-written object. Rejects files > **256 KB** and non-UTF-8 (binary) with an explanatory message; a path under a hidden subtree returns `Error: not available`. |
| `storage_status()` | Configured root + indexed file/dir counts. |
| `storage_rescan()` | Re-walk the tree and refresh the index. |
| `storage_object_put(key, content)` | Store a text object under `key` (tenant bucket) **and catalogue it** so it appears in the Files page and is searchable / readable / downloadable; a nested key (`reports/q2.md`) creates the folder tree. Returns the normalised key used. |
| `storage_object_get(key)` | Retrieve a stored object (or `null`). |

### HTTP

| Method · Path | Purpose |
| --- | --- |
| `POST /ingest?filename=…&att_id=…` | **Chat upload sink (ADR-0025).** Body is the raw file bytes; `Content-Type` carries the media type. Stores the bytes in the object store under `uploads/<att_id>-<name>`, catalogues them (browsable + downloadable), and returns `{key, name, size}`. Called by the core's attachment-upload route. |
| `GET /pages/files?path=…&q=…` | `BrowserData`-shaped payload for the Files left-nav page (ADR-0018). `path` browses a directory (empty = root); `q` runs a search. Proxied by the core at `GET /platform/v1/modules/storage/pages/files`. |
| `GET /read?path=…` | **Split-screen reader (#KB-refactor).** Return a UTF-8 text file's contents → `{path, name, content}` — a catalogued object (chat upload or agent-written) decoded from MinIO, or a file from the read-only tree. **400** traversal, **404** missing, **413** larger than 256 KB, **415** binary / non-UTF-8. Proxied by the core at `GET /platform/v1/modules/storage/read`. |
| `GET /download?path=…` | Stream a file (binary-safe) — a catalogued object (chat upload or agent-written) from MinIO, or a file from the read-only tree. Path-traversal attempts → **HTTP 400**. Proxied by the core at `GET /platform/v1/modules/storage/download`. |
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
| `STORAGE_ROOT` | `/data` | In-container **base** of the shared-file-space mount. Storage serves and indexes the tenant subtree `STORAGE_ROOT/<tenant>` read-only (tenant-scoped, constraint #1; `<tenant>` = `DEFAULT_TENANT_ID`). |
| `STORAGE_AGENT_HIDDEN_PREFIXES` | `notes` | Comma-separated top-level subtrees hidden from the **agent's** file tools (#KB-refactor). The agent's `storage_list`/`storage_search`/`storage_read` never see them; the operator-facing Files page / `/read` / `/download` are unaffected. `notes/` holds private note bodies. Set empty to hide nothing. |
| `DATABASE_URL` | `postgresql+asyncpg://…/epicurus` | The file index. |
| `MINIO_URL` | `http://minio:9000` | Object-store endpoint. |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | `epicurus` / `epicurus-dev` | Object-store creds (dev; OpenBao later). |

In the stack, the **shared file space** is bound to `/data` **read-only** via
`EPICURUS_FILES_ROOT` (the single env var that mounts the same `/data` tree across storage,
knowledge, and notes), which defaults to an **empty named volume** — nothing is exposed
until you point it at a real directory (never the host home dir). The on-disk tree is
**tenant-scoped** (constraint #1): storage serves `/data/<tenant>`, and the same volume is
mounted **read-write** by knowledge (`/data/<tenant>/knowledge`) and notes
(`/data/<tenant>/notes`), which own their subfolders; storage only indexes and serves the
tenant subtree. `EPICURUS_FILES_ROOT` **replaces** the old per-module `STORAGE_HOST_ROOT`. See
[Infrastructure](../infrastructure/index.md#shared-file-space).

## Data model

- **Postgres `storage_files`** — one row per indexed entry: `id`, `tenant`, `path`,
  `name`, `size`, `mtime`, `kind` (`file`/`dir`), `updated_at`, and `source`
  (`fs` = scanned, read-only · `object` = a MinIO-backed object — a chat upload **or** an
  agent-written file); unique on `(tenant, path)`. Tenant-scoped; a re-scan upserts and purges
  stale **`fs`** rows only, so object rows survive every scan. The `source` column is added in
  place at init on a pre-v0.3 deployment (no migration tool — the index uses `create_all`),
  backfilled to `fs`.
- **MinIO bucket `{tenant}-storage`** (`scope_bucket`) — app-managed objects, created
  lazily, one bucket per tenant. Chat uploads live here under the `uploads/` prefix; the
  `storage_object_*` tools store text objects in the same bucket under the agent's chosen key.
  Either way the object is catalogued in `storage_files` (a `source="object"` row plus ancestor
  folder rows) so it appears in the Files page (#347).

## Dependencies

Postgres (the file index) · MinIO (objects) · NATS (the scan event) · the read-only
mounted directory tree. It uses **no AI** — pure filesystem + object I/O.

## Run & extend

```bash
EPICURUS_FILES_ROOT=/path/to/your/files docker compose up -d storage
```

Package `epicurus_storage`: `scanner.py` (walk + incremental upsert), `db.py`
(`storage_files` + queries + `source` column), `object_store.py` (MinIO via aioboto3 —
text **and** binary `put_bytes`/`get_object`), `service.py` (the MCP tools + the
`hidden_prefixes` filter that keeps private subtrees out of the agent's file tools + manifest
UI + `build_page_data` + `ingest_object`/`put_object`/`load_object_download` + `load_text_file`
for the inline reader; `put_object` is the catalogue-on-write the `storage_object_put` tool wraps), `app.py` (lifespan + `/ingest` + `/download` + `/read` + `/pages/files`; parses
`STORAGE_AGENT_HIDDEN_PREFIXES` into the module's `hidden_prefixes`).
