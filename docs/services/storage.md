# storage — object store + chat-upload sink

**`epicurus-storage`** v0.9.0 is a sidecar module that owns **app-managed object storage**
in MinIO — the durable sink for files the platform itself creates (chat uploads, exports,
agent-written objects) — and exposes the agent's **file tools** (`storage_list` /
`storage_search` / `storage_read`) over the **core file space**. Host port **8083**.

The unified **Files** view — the browser page, the split-screen reader, and download — now
lives in the **core** (ADR-0063, building on ADR-0052): the core mounts the shared
`epicurus-files` volume, owns the file index over the swappable `FileStore`, and serves
`GET /platform/v1/files/*`. Storage **no longer mounts `/data`** and no longer runs a
directory scanner or watcher. Its agent file tools read the file space through the **core
file API** via `PlatformClient`, and its object-only HTTP endpoints (`GET /objects`,
`GET /objects/read`, `GET /download`, `POST /objects/move`, `DELETE /objects`) are proxied by
the core, which merges the storage objects into the core Files page. See
[file space](../reference/files.md).

**Private subtrees are hidden from the agent (#KB-refactor).** Some folders are
operator-only: the `notes/` mirror holds **private** note bodies the agent must never read.
The agent's **file tools** (`storage_list` / `storage_search` / `storage_read`) therefore
exclude the subtrees named in `STORAGE_AGENT_HIDDEN_PREFIXES` (default `notes`), while the
**operator-facing** core Files surface shows and reads everything. So a note stays browsable
and readable in the Files UI but invisible to the agent through the file tools (its content
reaches the agent only when the user attaches it; see [notes](notes.md)).

## What moved to the core (ADR-0063)

v0.8.0 hands the unified Files surface to the core (file-space migration Phase 2):

- **The Files browser page, the split-screen reader, and download** moved to the core,
  served at `GET /platform/v1/files/page`, `GET /platform/v1/files/read`, and
  `GET /platform/v1/files/download`. The core owns the **file index** over the file space
  and merges storage's objects into that page. Storage's old `GET /pages/files`, the
  filesystem `GET /read`, the filesystem `GET /download`, and `POST /pages/files/move` are
  **removed**.
- **The directory scanner + files-tree watcher** (the old `STORAGE_WATCH` machinery and the
  `storage_rescan` tool / "Re-scan now" action) are **removed** — the core now scans and
  watches the volume. `storage_status` reports object-store counts, no filesystem root.
- **Storage no longer mounts `/data`.** Its agent file tools call the core file API
  (`PlatformClient.files_*`) instead of walking a local mount.
- **Storage keeps** the MinIO object store, the chat-upload sink (`POST /ingest`), the
  `storage_object_put` / `storage_object_get` tools, and exposes **object-only** endpoints
  the core proxies and merges into Files: `GET /objects`, `GET /objects/read`,
  `GET /download`, `POST /objects/move`, `DELETE /objects`.

Earlier history: v0.3.0 added the **chat upload sink** (`POST /ingest`, ADR-0025) — files
attached in chat are durably persisted to the object store and appear under an **`uploads/`**
folder in the Files view. v0.5.x hid private subtrees from the agent's file tools and made
agent-written objects (`storage_object_put`) appear in Files. Those behaviours persist; only
the surface that *renders* Files (and the scanner/watcher that fed it) moved to the core.

## The contract it exposes

### MCP tools (agent-facing)

> The three **file** tools below read the **core file space** through `PlatformClient` and
> never see the subtrees in `STORAGE_AGENT_HIDDEN_PREFIXES` (default `notes`):
> `storage_list` / `storage_search` filter them out and `storage_read` refuses them with
> `Error: not available` (#KB-refactor). The operator-facing core Files surface is unaffected.

> A `path` may also address an operator-declared **external mount** (#731) via
> `mount:<name>/<sub-path>` (`mount:<name>` alone for its own root) — these tools pass
> whatever string they're given straight through to the core, which resolves the prefix. See
> [file space → External mounts](../reference/files.md#external-mounts-731).

| Tool | Purpose |
| --- | --- |
| `storage_list(path="")` | List the direct children of `path` in the file space (dirs before files), via `PlatformClient.files_list`. A hidden subtree (e.g. `notes/`) yields nothing; the root listing also includes any declared external mount as a folder. |
| `storage_search(query, limit=50)` | Case-insensitive name/path search (max 200) over the core file index, via `PlatformClient.files_search`. Hits under a hidden subtree are filtered out; covers every external mount that opted into indexing. |
| `storage_read(path)` | Return a text file's contents — a file-space file (via `PlatformClient.files_read`) **or** an agent-written object. Rejects files > **256 KB** and non-UTF-8 (binary) with an explanatory message; a path under a hidden subtree returns `Error: not available`. |
| `storage_status()` | Object-store counts (catalogued objects), tenant-scoped. No filesystem root. |
| `storage_object_put(key, content)` | Store a text object under `key` (tenant bucket) **and catalogue it** so it appears in the core Files page and is searchable / readable / downloadable; a nested key (`reports/q2.md`) creates the folder tree. Returns the normalised key used. |
| `storage_object_get(key)` | Retrieve a stored object (or `null`). |

### HTTP

All paths below are object-store-only — they list, read, move, and stream the module's MinIO
objects (chat uploads + agent-written files). The core proxies them and **merges** the objects
into the unified Files page; the operator never calls storage directly.

| Method · Path | Purpose |
| --- | --- |
| `POST /ingest?filename=…&att_id=…` | **Chat upload sink (ADR-0025).** Body is the raw file bytes; `Content-Type` carries the media type. Stores the bytes in the object store under `uploads/<att_id>-<name>`, catalogues them (browsable + downloadable), and returns `{key, name, size}`. Called by the core's attachment-upload route. |
| `GET /objects?path=…&q=…` | **Object list / search.** Returns `{entries: [{path, name, size, mtime, kind}]}` — the catalogued objects under `path` (empty = root), or a name/path search when `q` is set. The core fetches this to merge objects into `GET /platform/v1/files/page` (and to back object-name results in `GET /platform/v1/files/search`). |
| `GET /objects/read?path=…` | **Object text read.** Return a UTF-8 text object's contents → `{path, name, content}`. **400** traversal, **404** missing, **413** larger than 256 KB, **415** binary / non-UTF-8. The core calls this when a Files read targets a storage object. |
| `GET /download?path=…` | **Object-only streaming** (binary-safe) — streams a catalogued object from MinIO with its stored content type. Path-traversal attempts → **HTTP 400**, **404** when the object is not catalogued. The core's `GET /platform/v1/files/download` proxies here for object entries (file-space files stream from the core's own store). |
| `POST /objects/move` (body `{from_path, to_path}`) | **Rename/move an object (#381 / #391).** Object entries only. **404** missing src, **409** dst occupied, **400** traversal. Returns `{path}`. The core invokes this when a Files move targets a storage object. |
| `DELETE /objects?path=…` | **Delete an object and its subtree (#564).** Object entries only. Returns `{deleted}` — idempotent (a missing object is a clean `false`, not a 404). **400** the root or a read-only (`source="fs"`) entry. The core's `DELETE /platform/v1/files/entry` falls back here when a Files-page delete targets a storage object (chat upload / agent object). |
| `GET /export?tenant_id=…` · `POST /import?tenant_id=…&dry_run=…` | **Tenant portability, the catalogue half (#876)** — NDJSON records, served by `add_portability_routes`. See *Portability* below. |
| `GET /export/blobs?tenant_id=…` · `GET /export/blobs/{id}?tenant_id=…` · `PUT /import/blobs/{id}?tenant_id=…&sha256=…&size=…&content_type=…` | **Tenant portability, the byte half (#876)** — the objects themselves, listed and streamed. Storage is the only module that serves these today. |
| `GET /health` · `GET /metrics` · `GET /manifest` | Ops + the module manifest. |

> **Path safety.** Object paths are normalised and confined to the tenant bucket
> (`scope_bucket`), rejecting `..`, absolute paths, and escapes. The objects live in the
> writable, tenant-scoped object bucket; the read-only file-space tree is the core's concern.

### Events (NATS)

None. The module no longer scans a directory, so the former `<tenant>.storage.scan.completed`
event is removed (the core owns the file index and its scan now).

### Web UI (manifest)

Folder icon and a **Show status** action (object-store counts) — auto-rendered by the shell
(ADR-0007). The former config form for the storage root and the **Re-scan now** action are
gone (no root, no scanner). The **Files** left-nav page is now a **core-owned** top-level
surface (like Models / Observability / Modules), not a module page — see
[file space](../reference/files.md) and [web](web.md).

### The chat upload sink (ADR-0025)

When a user attaches a file in chat, the core's upload route keeps its core-side handle
**and** best-effort POSTs the bytes to this module's `POST /ingest`. The module:

1. **Stores the bytes** in the object bucket under `uploads/<att_id>-<name>` — the core
   attachment id makes the key unique, so two uploads of the same filename never collide.
2. **Catalogues** the upload in `storage_files` with an `uploads` directory row, so the
   core's Files page shows it (and `storage_search` finds it). The bytes live in MinIO; the
   catalogue row is metadata pointing at them.
3. **Serves it back**: `GET /download` and `GET /objects/read` resolve the catalogued object
   and stream / decode it from MinIO; the core proxies both for object entries in the Files
   view.

Tenant scoping holds end to end: the bytes land in the `{tenant}-storage` bucket and the
catalogue rows are tenant-scoped. The core treats persistence as **best-effort** — a down or
absent storage module never fails a chat upload.

### Rename / move objects (#381 / #391)

`POST /objects/move` (`service.move_item`) relocates a writable object through the
`{from_path, to_path}` → `{path}` contract knowledge and notes also expose (ADR-0059), so the
core drives all three the same way through the unified Files page:

- **Object entries only.** Only a catalogued object (a chat upload or an agent-written file)
  moves — its bytes live in the writable tenant bucket. A move that targets a read-only
  file-space file is handled by the core, not here.
- **Two stores, kept consistent.** MinIO holds the bytes; the catalogue is what the Files
  page and `/download` read. So `move_item` **copies** each object to its new key,
  **re-paths** the catalogue subtree in one transaction, then **deletes** the originals. A
  crash between steps leaves harmless orphan copies in MinIO, never a catalogue row pointing
  at missing bytes.
- **Errors.** **404** missing source · **409** destination occupied · **400** traversal. A
  move into a brand-new folder creates its navigable dir rows.

### Delete objects (#564)

`DELETE /objects` (`service.delete_item`) removes a writable object and its subtree — the
symmetric counterpart to move, the fallback the core's Files-page delete door uses when a path
is not in the core file space:

- **Object entries only.** Like move, only a catalogued object (chat upload / agent-written
  file) is deletable; a read-only (`source="fs"`) entry is refused (**400**), and the file-space
  root is refused (**400**). A file-space file's delete is handled by the core, not here.
- **Byte store first, catalogue last.** `delete_item` drops every object's bytes from MinIO,
  then removes the catalogue subtree (`FileIndex.delete_subtree`) in one commit. A crash between
  steps leaves harmless orphan catalogue rows pointing at absent bytes — a rescan never revisits
  `object` rows, so the operator simply deletes again; it never strands live, downloadable bytes.
- **Idempotent.** Nothing at the path is a clean `{deleted: false}`, not a 404 — matching the
  `FileStore.delete` seam, so the core can tell "nothing here" from a real failure.

## Configuration

`StorageSettings` extends [`CoreSettings`](../reference/config.md):

| Env var | Default | Meaning |
| --- | --- | --- |
| `PLATFORM_URL` | `http://core-app:8080` | The core base URL. The agent file tools read the file space through the core file API (`PlatformClient.files_*`) against this URL — storage no longer mounts `/data`. |
| `STORAGE_AGENT_HIDDEN_PREFIXES` | `notes` | Comma-separated top-level subtrees hidden from the **agent's** file tools (#KB-refactor). The agent's `storage_list` / `storage_search` / `storage_read` never see them; the operator-facing core Files surface is unaffected. `notes/` holds private note bodies. Set empty to hide nothing. |
| `DATABASE_URL` | `postgresql+asyncpg://…/epicurus` | The object catalogue. |
| `MINIO_URL` | `http://minio:9000` | Object-store endpoint. |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | `epicurus` / `epicurus-dev` | Object-store creds (dev; OpenBao later). |

`STORAGE_ROOT`, `STORAGE_WATCH`, and `STORAGE_WATCH_DEBOUNCE_MS` are **removed** — storage no
longer mounts or scans the shared file space (the core does, behind `FILES_WATCH` /
`FILES_WATCH_DEBOUNCE_MS`; see [config](../reference/config.md) and
[file space](../reference/files.md)).

## Data model

- **Postgres `storage_files`** — the object catalogue: one row per catalogued object —
  `id`, `tenant`, `path`, `name`, `size`, `mtime`, `kind` (`file`/`dir`), `updated_at`, and
  `source` (`object` = a MinIO-backed object); unique on `(tenant, path)`. Tenant-scoped. Each
  MinIO-backed object (a chat upload **or** an agent-written file via `storage_object_put`) gets
  a `source="object"` row plus its ancestor folder rows, so it appears and is searchable in the
  core Files page. Storage no longer scans the filesystem, so it writes only `object` rows now —
  the core owns the file-space (`fs`) index.
- **MinIO bucket `{tenant}-storage`** (`scope_bucket`) — app-managed objects, created
  lazily, one bucket per tenant. Chat uploads live here under the `uploads/` prefix; the
  `storage_object_*` tools store text objects in the same bucket under the agent's chosen key.
  Either way the object is catalogued in `storage_files` so the core Files page lists it.

## Portability (#876)

Storage is the one module whose export is **bytes**. Every other portable module carries rows;
here the rows are only half the story — the objects live in the tenant's own MinIO bucket,
which is *not* part of the core-owned file space the archive already carries under `files/`.
Lose the bytes and an operator moves house to a list of filenames; lose the rows and the bytes
land in a bucket nothing can see. So the module implements both halves of the
[portability contract](../reference/modules.md#portability--get-export--post-import-867):
`PortabilityStore` for the catalogue and `BlobPortabilityStore` for the objects.

**Schema `storage/1`.** `EpicurusModule(portable=True)`; the store is
`epicurus_storage/portability.py`, wired in `app.py`.

| Kind | Stable id | What travels |
| --- | --- | --- |
| `file` | the object **path** (`uploads/a1-scan.pdf`) — the natural key, unique per tenant, and the same id its blob uses | `name` · `size` · `mtime` |
| `folder` | the folder **path** (`uploads`) | `name` |
| *blob* | the same object path as the `file` record it belongs to | the object's bytes, with the `content_type` MinIO holds them under |

Folders travel as records of their own rather than being re-derived from file paths on import:
deriving them would be nearly right and quietly wrong, since a folder the operator created and
then emptied has no file under it to derive it from.

**Excluded**

| Left out | Why |
| --- | --- |
| `storage_files` rows with `source="fs"` | **derived** — the legacy mirror of the core-owned file space (ADR-0063), which the archive carries itself under `files/`. Exporting them would carry that tree twice, and carry it as *this* module's source of truth when it is not. |
| the surrogate `storage_files.id` | a per-database autoincrement means nothing in another installation; the path is the natural key the upsert matches on. |
| `tenant` | never travels — stripped on export, re-applied from the **target** tenant on import (constraint #1). |
| `source` | implied: everything exported here is an object row, and the import writes it back as one. |
| `updated_at` | **operational** — when *this* installation last wrote the row. It says nothing about the file, and carrying it would make an unchanged re-import look like a change. |
| the MinIO bucket's own state (bucket policy, versioning, an uncatalogued object) | **not source of truth** — the catalogue is what every surface reads, so an object with no row is unreachable here and would arrive unreachable. |
| secrets | never — the module holds none (ADR-0010/0020). |

**Rules it obeys.** Import is an upsert by path and **never deletes**. A second apply is a
no-op: an identical row is `skipped`, and a blob is skipped when the object already at that id
hashes to the same SHA-256. **Nothing is ever overwritten** — an id holding *different* bytes
keeps them and is reported as a conflict, the same rule the core applies to a file-space file
the operator has since edited. A row this module does not own (a read-only `source="fs"` entry
at the same path) is skipped rather than rewritten. An unknown `kind` is `skipped` with a
warning. Nothing is ever held whole in memory: objects are read with `ObjectStore.open_object`
and written with `ObjectStore.put_stream`, which switches to a multipart upload mid-stream (so
a transfer that fails its digest check aborts without publishing a byte).

**A record without its bytes.** The per-blob ceiling (`PORTABILITY_MAX_FILE_MB`) applies to each
object. An over-cap object is omitted from the archive and **named** in the export job's reason
and in the import report's `missing` list — but **its catalogue record still travels**. After
the import the file is listed in the Files page with its size and its download answers **404**
until the operator copies those bytes across by hand. That is deliberate: dropping the row
instead would leave no trace that the file ever existed, and nothing for the operator to go and
fetch. The same applies to an object whose bytes are already gone on the source — its row
travels, its blob is not listed.

## Dependencies

Postgres (the object catalogue) · MinIO (objects) · the **core** (`PLATFORM_URL`) — the agent
file tools read the file space through the core file API (`PlatformClient.files_*`), and the
core proxies storage's object endpoints into the unified Files page. It uses **no AI** — pure
object I/O plus the core file API.

## Run & extend

```bash
docker compose up -d storage
```

Storage no longer needs a `/data` mount; it reaches the file space through the core
(`PLATFORM_URL`). The shared `epicurus-files` volume is mounted by the **core** (and still by
knowledge, read-only); **notes** has also dropped its mount (#357/ADR-0065) and writes its `.md`
mirror through the core file API — see [Infrastructure](../infrastructure/index.md#shared-file-space).

Package `epicurus_storage`: `db.py` (`storage_files` catalogue + queries + `subtree`/`repath`
for the move re-key + `delete_subtree` for the delete de-key), `object_store.py` (MinIO via
aioboto3 — text **and** binary `put_bytes` / `get_object` + `copy` / `delete` for the object
move and delete), `service.py` (the MCP tools — the file tools call `PlatformClient.files_*`, the
`hidden_prefixes` filter keeps private subtrees out of those tools — plus `ingest_object` /
`put_object` / `load_object_download` / `load_object_text` / `move_item` / `delete_item` over the
object store; `put_object` is the catalogue-on-write the `storage_object_put` tool wraps),
`app.py` (`/ingest` + `/objects` + `/objects/read` + `/download` + `/objects/move` +
`DELETE /objects`; parses `STORAGE_AGENT_HIDDEN_PREFIXES` into the module's `hidden_prefixes` and
constructs the `PlatformClient` from `PLATFORM_URL`).
