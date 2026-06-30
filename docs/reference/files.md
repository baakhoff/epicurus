# File space (`epicurus_core.files`) — the core-owned, swappable file store

**What it is.** The per-tenant user file space, owned by the **core** behind a swappable
backend (ADR-0052). One `FileStore` interface, tenant-scoped on every call (constraint #1),
with a local-filesystem backend (self-host) and an S3/MinIO backend (SaaS) behind the same
contract — so no module hardcodes where files live (constraint #3). Modules read and write the
space through the platform API (`/platform/v1/files/*`) via `PlatformClient`, instead of mounting
the shared `/data` volume and doing their own I/O.

The **core mounts** the shared `epicurus-files` volume at `/data`, provisions the tenant root,
and owns a **file index** over the `FileStore` — it scans the tree at startup and watches it for
changes (debounced incremental rescan, `FILES_WATCH` / `FILES_WATCH_DEBOUNCE_MS`). The unified
**Files** browser, the split-screen reader, and download are now **core-owned**, served at
`GET /platform/v1/files/{page,search,download}` (ADR-0063), and the storage module's objects are
**merged in** so chat uploads and agent-written files show alongside the file-space tree.

> **Phased rollout (ADR-0052 → ADR-0065).** This page documents **Phases 1–4**: the abstraction
> and wire contract (Phase 1), the core taking ownership of the volume mount, the file index, and
> the Files browser / read / download with the storage module reading through this API (Phase 2),
> the **knowledge** module routing its writes through this API while its mount drops to
> read-only (Phase 3, #356/ADR-0064), and the **notes** module routing its `.md`-mirror writes
> through this API and **dropping its `/data` mount entirely** (Phase 4, #357/ADR-0065 — see the
> phase plan below). The one-shot **`files-init`** provisioning bootstrap remains (its retirement —
> the last bit of #357 — is a follow-up that moves a surgical volume-chown into the core image's
> entrypoint), so the file space is core-owned but the provisioning container still runs.

## The epicurus-core API

Importable from the top level: `from epicurus_core import FileStore, FileEntry, build_file_store`.

### `FileEntry`

A node in the tenant file space — `{path, name, kind: "file"|"dir", size, mtime}`. `path` is the
tenant-relative POSIX path (no leading slash, no tenant segment); `size`/`mtime` are `0` for
directories and for backends that do not report them.

### `FileStore` (abstract)

Tenant-scoped read / write / list / delete behind one interface. Every method takes `tenant`
explicitly (constraint #1). Missing paths raise `FileNotFoundError`.

| Method | Purpose |
| --- | --- |
| `read_bytes(tenant, path) -> bytes` | Raw bytes; raises `FileNotFoundError`. |
| `write_bytes(tenant, path, data, content_type=None) -> FileEntry` | Write, creating parents. |
| `read_text` / `write_text` | UTF-8 convenience; `read_text` caps at **256 KB** and raises on binary. |
| `list_dir(tenant, path="") -> list[FileEntry]` | Direct children (dirs before files). |
| `stat(tenant, path) -> FileEntry \| None` | The entry, or `None`. |
| `delete(tenant, path) -> bool` | Delete a file or directory tree; the tenant root is rejected. |
| `ensure_dir(tenant, path) -> FileEntry` | Create a directory (and parents). |
| `move(tenant, src, dst) -> FileEntry` | Move/rename a file or tree (rename = same-parent move); raises `FileNotFoundError` (missing src), `FileExistsError` (dst occupied), `ValueError` (root / into-itself). |
| `ensure_tenant_root(tenant)` | Provision the tenant root (core-owned provisioning). |

Path-safety is centralized in `normalize_rel()`: it collapses `\`, `//`, and `.`, and **rejects**
any `..` segment, so a key can never escape its tenant root.

### Backends + `build_file_store`

- **`LocalFileStore(root)`** — the tenant tree under `<root>/<tenant>` (the self-host default).
  Blocking disk I/O runs in a worker thread so the event loop stays free.
- **`S3FileStore(url, access_key, secret_key)`** — keys in a `{tenant}-files` bucket
  (`scope_bucket`); directories are virtual, listed via the `/` delimiter. Needs `aioboto3`
  (install the `epicurus-core[s3]` extra).
- **`build_file_store(backend, root, s3_url, s3_access_key, s3_secret_key)`** — the single swap
  point for constraint #3: `local` (default) or `s3`.

## The wire contract (`/platform/v1/files/*`)

The core mounts these; modules call them through `PlatformClient`. `tenant_id` defaults to the
core's tenant when omitted; tenant scoping is enforced on every call.

| Method · Path | Purpose |
| --- | --- |
| `GET /platform/v1/files/page?path=&q=&tenant_id=` | **Files browser page (ADR-0063).** `BrowserData` for the `browser` archetype — merges the file-space tree (the core file index) with the **storage module's objects** (chat uploads, agent-written files). `path` browses a directory (empty = root); `q` runs a name/path search. The shell renders the core-owned **Files** surface from this. |
| `GET /platform/v1/files/search?q=&limit=&tenant_id=` | `{entries: [FileEntry]}` — name/path search over the core file index (merged with object names); backs `PlatformClient.files_search`. |
| `GET /platform/v1/files/download?path=&tenant_id=` | Stream a file (binary-safe). **File-space first**, else proxies the storage object store (`GET /download` on the storage module) for object entries. **400** traversal, **404** missing. |
| `GET /platform/v1/files/list?path=&tenant_id=` | `{entries: [FileEntry]}` — children of `path` (empty = root). |
| `GET /platform/v1/files/read?path=&tenant_id=` | `{path, name, content}` — UTF-8 text. **File-space first**, else falls back to the storage object store (an object's text). **404** missing, **413** > 256 KB, **415** binary, **400** traversal. |
| `GET /platform/v1/files/stat?path=&tenant_id=` | A `FileEntry`, or **404**. |
| `PUT /platform/v1/files/write?path=&tenant_id=` (body `{content}`) | Write UTF-8 text → `FileEntry`. **400** writing the root. |
| `DELETE /platform/v1/files?path=&tenant_id=` | `{deleted}` — a file or a whole tree. **400** deleting the root. |
| `POST /platform/v1/files/dir?path=&tenant_id=` | Create a directory → `FileEntry`. |
| `POST /platform/v1/files/move?tenant_id=` (body `{src, dst}`) | Move/rename → `FileEntry`. **File-space first**, else falls back to the storage object store (`POST /objects/move`) for object entries. **404** missing src, **409** dst exists, **400** root/traversal/into-itself. |

> **`read`, `move`, and `download` fall back to the storage object store** for object entries
> (chat uploads, agent-written files): the core tries the file space first, then proxies the
> storage module (`GET /objects/read`, `POST /objects/move`, `GET /download`) so a unified Files
> read / move / download spans both stores. `page`/`search` merge the two for listing.

### `PlatformClient` methods

`files_list(path="")`, `files_read(path)`, `files_search(q, limit=50)`, `files_write(path, content)`,
`files_stat(path)`, `files_delete(path)`, `files_make_dir(path)`, `files_move(src, dst)` — the typed
module-side consumer of the endpoints above (`files_stat` returns `None` on 404; `files_search`
returns `list[FileEntry]` over the core file index, used by storage's `storage_search` tool;
`files_move` raises on 404/409/400).

## Configuration (core-app)

| Setting | Env | Default | Meaning |
| --- | --- | --- | --- |
| `files_backend` | `FILES_BACKEND` | `local` | `local` (filesystem) or `s3` (MinIO/S3). |
| `files_root` | `FILES_ROOT` | `/data` | Local-backend base; the tenant tree is `FILES_ROOT/<tenant>`. |
| `files_s3_url` | `FILES_S3_URL` | `http://minio:9000` | S3 endpoint (when `files_backend=s3`). |
| `files_s3_access_key` / `files_s3_secret_key` | `FILES_S3_ACCESS_KEY` / `FILES_S3_SECRET_KEY` | `epicurus` / `epicurus-dev` | S3 credentials (dev defaults; OpenBao later). |
| `files_watch` | `FILES_WATCH` | `true` | Watch the mounted file space and **incrementally rescan on change** (create/modify/delete) so files another module or an external write lands after startup show up in the Files page and search without a restart (ADR-0063). On by default. Set `false` to keep startup-only scanning. |
| `files_watch_debounce_ms` | `FILES_WATCH_DEBOUNCE_MS` | `1500` | Coalescing window (ms) for a burst of file changes before a watch-triggered rescan fires; a module dropping many files at once is grouped into one incremental pass. |

## Data model

Per-tenant scoping (constraint #1): the local backend writes `<root>/<tenant>/…`; the S3 backend
uses a `{tenant}-files` bucket. The backend (the filesystem or the object bucket) *is* the store
for bytes. On top of it the core owns a **unified file index** (ADR-0063) — a tenant-scoped catalogue
of the file-space tree, populated by the startup scan and kept current by the watcher — that backs
`GET /platform/v1/files/page` and `…/search` (the storage module's objects are merged in at request
time, not stored in this index).

## Dependencies

Local backend: the filesystem (the core mounts the shared `/data` volume). S3 backend: `aioboto3`
against a MinIO/S3 endpoint. The Files page / read / move / download **merge with or fall back to**
the **storage module** for object entries (chat uploads, agent-written files), proxied over the
internal network. Uses **no AI**.

## Phase plan (ADR-0052 → ADR-0065)

- **Phase 1 (done):** the `FileStore` abstraction, the `/platform/v1/files/*` contract,
  `PlatformClient.files_*`, and core-side provisioning. Additive — the modules were unchanged.
- **Phase 2 (done, ADR-0063):** the core mounts + provisions the shared volume, owns a unified
  file index (startup scan + watcher), and the Files browser / read / download move to the core
  (`/platform/v1/files/{page,search,download}`); the storage module reads the file space through
  this API and contributes its objects (read/move/download fall back to it).
- **Phase 3 (done, #356/ADR-0064):** the **knowledge** module is now a **write-consumer** of
  the file API — after storage (Phase 2), it is the second module to route its writes through
  `PlatformClient.files_*` (the editor save, the file-tree CRUD, and the agent's approved
  suggestions; a vault path maps to the core path `knowledge/<rel>`). Its `/data` mount drops to
  **read-only** (reads + the incremental indexer + the #232 watcher stay on it); a follow-up
  migrates the reads off the mount so it can be dropped entirely.
- **Phase 4 (done, #357/ADR-0065):** the **notes** module is now the **third write-consumer** of
  the file API — after storage (Phase 2) and knowledge (Phase 3). Its `.md` mirror (`write` /
  `delete` / startup `backfill`) routes through `PlatformClient.files_*` at core path
  `notes/<rel>`, and notes **drops its `/data` mount entirely** (it reads nothing from disk — the
  indexer and editor read Postgres — and drops its `files-init` dependency). Postgres stays the
  source of truth; the mirror is write-only output. **`files-init` is not yet retired** — that last
  bit of #357 is a separate infra follow-up (move a surgical volume-chown into the core image's
  entrypoint), so the file space is core-owned but the provisioning bootstrap container remains.

## Run & extend

The store is constructed in `create_app()` via `build_file_store(...)` from the `FILES_*` settings
and mounted by `create_files_router` (`epicurus_core_app/files_routes.py`); the core also mounts the
shared `/data` volume, provisions the tenant root, and starts the file index (startup scan + the
`FILES_WATCH` watcher). A new backend implements `FileStore` and is selected in `build_file_store`.
When adding an endpoint, extend the router and the matching `PlatformClient.files_*` method together
so the contract stays symmetric.
