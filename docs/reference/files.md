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
> the **knowledge** module routing its writes through this API — then its reads too, **dropping
> its `/data` mount** in normal mode (Phase 3 + tail, #356/ADR-0064 + #346/ADR-0070), and the
> **notes** module routing its `.md`-mirror writes through this API and **dropping its `/data`
> mount entirely** (Phase 4, #357/ADR-0065 — see the phase plan below). The tenant-root chown now
> lives in the **core image's entrypoint** (#421/ADR-0069), retiring the old `files-init` one-shot:
> the file space is fully core-owned, the **core is the sole mounter of `/data`**, and no module
> mounts it (watch-mode knowledge is the lone opt-in exception — an external Obsidian vault on a
> disk mount, not the core-owned space).

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

- **`LocalFileStore(root, *, tenant_subdir=True)`** — the tenant tree under `<root>/<tenant>`
  (the self-host default). Blocking disk I/O runs in a worker thread so the event loop stays
  free. `tenant_subdir=False` (#731) addresses *root* itself rather than `root/<tenant>` — the
  seam an [external mount](#external-mounts-731) uses, since a mount root already names one
  directory; `tenant` is still validated on every call even though it no longer shapes the
  path.
- **`S3FileStore(url, access_key, secret_key)`** — keys in a `{tenant}-files` bucket
  (`scope_bucket`); directories are virtual, listed via the `/` delimiter. Needs `aioboto3`
  (install the `epicurus-core[s3]` extra).
- **`build_file_store(backend, root, s3_url, s3_access_key, s3_secret_key)`** — the single swap
  point for constraint #3: `local` (default) or `s3`.

### `PathEscapeError`

A `ValueError` subclass `LocalFileStore` raises when a *resolved* path escapes the store's
root — a symlink inside the tree pointing outward (`normalize_rel` already rejects a literal
`..` in the request path; this is the runtime backstop for what only a real filesystem entry
can produce). `files_routes.py` maps it to a clean **400** via an app-level exception handler
(`app.py`, alongside `GatewayPausedError`) rather than a 500, so a handler with no local
`ValueError` catch (`list`, `stat`, `dir`, `upload`, `page`, `download`) still fails cleanly.
Existing `except ValueError` handlers are unaffected — it *is* a `ValueError`.

## The wire contract (`/platform/v1/files/*`)

The core mounts these; modules call them through `PlatformClient`. `tenant_id` defaults to the
core's tenant when omitted; tenant scoping is enforced on every call.

| Method · Path | Purpose |
| --- | --- |
| `GET /platform/v1/files/page?path=&q=&tenant_id=` | **Files browser page (ADR-0063).** `BrowserData` for the `browser` archetype — merges the file-space tree (the core file index) with the **storage module's objects** (chat uploads, agent-written files). `path` browses a directory (empty = root); `q` runs a name/path search. The shell renders the core-owned **Files** surface from this. **Movability (#479):** object entries and operator-space file-space *files* are `movable`; directories and anything under a module-owned top-level folder (the `module_urls` hostnames — `knowledge/…`, `notes/…`) are read-only in the UI. **Dedupe (#560):** a node reported by **both** sources (same `kind` + normalized path) renders **once** — the file-space entry wins, so its movability is authoritative rather than an object duplicate forcing `movable=True`. Applies to both browse and search. |
| `POST /platform/v1/files/upload?dir=&tenant_id=` (multipart `file`) | **Upload into the file space (#479)** — the Files page's upload door (one file per request; the UI sequences multi-picks for per-file progress). Lands `dir/<filename>` through the FileStore seam and **indexes it immediately** (listed + searchable, no rescan). Enforces the shared #175 caps: **415** type not in `ATTACHMENT_ALLOWED_TYPES`, **413** over `ATTACHMENT_MAX_BYTES`. **400** traversal or a module-owned `dir`. A name collision gets a `-2`/`-3`… suffix, never an overwrite. Returns the written `FileEntry`. Operator-UI-facing: modules keep writing via `PUT …/write` / `PlatformClient.files_write`, so there is deliberately no `files_upload` client method. |
| `DELETE /platform/v1/files/entry?path=&tenant_id=` | **Delete from the file space (#564)** — the Files page's delete door, the operator counterpart to the module-facing `DELETE …/files` (mirrors `upload` vs `write`). `{deleted}` — recursive for a folder, hard (no trash/undo). **File-space first**, else falls back to the storage object store (`DELETE /objects`) for object entries; **de-indexes immediately** (drops from search/listing at once, the #390 watcher is the backstop). **400** deleting the tenant root, **or** a path under a module-owned top-level folder (`knowledge/…`, `notes/…`) — that lifecycle belongs to the owning page (#216/#340), enforced server-side so a crafted request can't bypass the hidden button. Operator-UI-facing (`filesDelete` in the web app); modules keep deleting **inside their own subtrees** via the unguarded `DELETE …/files` / `PlatformClient.files_delete`, so there is deliberately no operator-guard on that door. |
| `GET /platform/v1/files/search?q=&limit=&tenant_id=` | `{entries: [FileEntry]}` — name/path search over the core file index (merged with object names); backs `PlatformClient.files_search`. |
| `GET /platform/v1/files/download?path=&tenant_id=` | Stream a file (binary-safe). **File-space first**, else proxies the storage object store (`GET /download` on the storage module) for object entries. **400** traversal, **404** missing. |
| `GET /platform/v1/files/list?path=&tenant_id=` | `{entries: [FileEntry]}` — children of `path` (empty = root). |
| `GET /platform/v1/files/read?path=&tenant_id=` | `{path, name, content}` — UTF-8 text. **File-space first**, else falls back to the storage object store (an object's text). **404** missing, **413** > 256 KB, **415** binary, **400** traversal. |
| `GET /platform/v1/files/stat?path=&tenant_id=` | A `FileEntry`, or **404**. |
| `PUT /platform/v1/files/write?path=&tenant_id=` (body `{content}`) | Write UTF-8 text → `FileEntry`. **400** writing the root. |
| `DELETE /platform/v1/files?path=&tenant_id=` | `{deleted}` — a file or a whole tree. **400** deleting the root. |
| `POST /platform/v1/files/dir?path=&tenant_id=` | Create a directory → `FileEntry`. |
| `POST /platform/v1/files/move?tenant_id=` (body `{src, dst}`) | Move/rename → `FileEntry`. **File-space first**, else falls back to the storage object store (`POST /objects/move`) for object entries. **404** missing src, **409** dst exists, **400** root/traversal/into-itself, a **module-owned `dst`** (its top-level segment is a `module_urls` hostname and differs from `src`'s top — mirrors the upload lock so a foreign file can't land behind a module's back, #479/#554; a module's *own* same-top move is still allowed), or a **pathological name** (a control char / NUL, or a path segment over 255 bytes — clamped to a clean 400 instead of a store-level 500). |
| `GET /platform/v1/files/scan-status?tenant_id=` | **Mass de-index fuse state (#848)** — `{fuse_enabled, max_delete_ratio, min_deletions, tripped, namespaces:[{tenant, namespace, indexed_rows, would_delete, seen_entries, reason, at}]}`. Scoped to `tenant_id` (default tenant when omitted): `tripped` and `namespaces` describe that tenant alone, while the three threshold fields are process-wide policy. A listed namespace is one whose index purge the scan is **refusing**: the file space read empty (or nearly so) while its rows are intact, so the rows were **kept**. `namespace` is `tenant` for the tenant tree, or `mount:<name>/` for a mount. |
| `POST /platform/v1/files/rescan?namespace=&force=&tenant_id=` | **Re-run the file-space scan (#848)** → `{namespace, entries, forced, tripped}`. The recovery door after a suspect mount: repair the mount, call this, and the index converges again. `namespace` empty = the tenant tree, otherwise an **indexed mount's name** (**404** for an unknown or un-indexed one). `force=true` purges the stale rows the fuse is withholding — the only way to make the index follow a file space that genuinely lost most of its contents. Runs for `tenant_id` (default tenant when omitted), under the same lock as the startup walk and the watcher, and `tripped` reports that tenant's verdict for that namespace. |

> **`read`, `move`, `download`, and `delete` fall back to the storage object store** for object
> entries (chat uploads, agent-written files): the core tries the file space first, then proxies
> the storage module (`GET /objects/read`, `POST /objects/move`, `GET /download`, `DELETE /objects`)
> so a unified Files read / move / download / delete spans both stores. `page`/`search` merge the
> two for listing.

**A 404's `detail` names the path and a recovery step (#742)** — `read`/`stat`/`move`'s missing-
entry cases read `"<path>" does not exist — it may have been deleted or moved; list the folder
for current contents` (`move`'s prefixes it `source ...`, since the operation has two paths).
A bare `"not found"` gives a caller — an agent directly, or a module tool that forwards this
text verbatim — nothing to act on; naming the path and suggesting a re-list closes that gap.
`delete`'s two doors stay idempotent on a miss (`{deleted: false}`, never a 404) — a deliberate,
pre-existing design, not something this issue changed: "delete something that's already gone"
is success, not an error, so there is no not-found *message* on that path to improve.

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
| `files_scan_fuse_enabled` | `FILES_SCAN_FUSE_ENABLED` | `true` | The **mass de-index fuse** (#848): refuse a scan's purge when the rows it would delete look wholesale rather than editorial, so a stale or empty mount cannot silently reconcile `core_files` to zero (the 2026-08-30 incident). Set `false` to restore the pre-#848 behaviour. |
| `files_scan_fuse_max_delete_ratio` | `FILES_SCAN_FUSE_MAX_DELETE_RATIO` | `0.5` | Share of a namespace's indexed rows whose purge in one scan is treated as suspect. |
| `files_scan_fuse_min_deletions` | `FILES_SCAN_FUSE_MIN_DELETIONS` | `5` | Absolute floor for the ratio rule — fewer stale rows never trip it, so a small tree stays prunable. A store that lists **nothing at all** while rows exist trips at any size. |

The **upload route shares the chat-attachment caps** (#175 → #479): `ATTACHMENT_MAX_BYTES`
(default 10 MiB → 413) and `ATTACHMENT_ALLOWED_TYPES` (default `text/*,image/*,application/pdf,
application/json` → 415) — one policy for every byte an operator puts into epicurus. The web
container's nginx fronts both routes with `client_max_body_size 12m` (keep it ≥ the byte cap).

## External mounts (#731)

**What.** An operator-declared, drive-style root beside the tenant file space — bind a host
directory (or an entire drive) into the container and it shows up in Files as an additional
top-level root, addressable by the operator (Files page) and the agent (`storage_list` /
`storage_search` / `storage_read`) exactly like the tenant tree, subject to a per-mount
read-only/read-write gate. See [Infrastructure → External file
mounts](../infrastructure/index.md#external-file-mounts-731) for the compose overlay that
declares one; this section covers the resulting API/agent-facing behavior.

**Declaration** (`CoreAppSettings`, all string env vars — an operator-level deployment concern,
not a per-request one):

| Setting | Env | Default | Meaning |
| --- | --- | --- | --- |
| `files_external_mounts` | `FILES_EXTERNAL_MOUNTS` | `""` | Comma-separated `name:container-path[:ro\|rw]` entries. `name` is 1-63 chars, lowercase alphanumeric plus `-`/`_` (it appears in URLs and index rows); mode defaults to `ro`. A malformed entry, a duplicate name, or an invalid mode fails startup (`epicurus_core_app.mounts.parse_mount_specs`) — loud, not a silently-wrong grant. |
| `files_external_mounts_indexed` | `FILES_EXTERNAL_MOUNTS_INDEXED` | `""` | Comma-separated mount *names* opted into indexing/watching. A mount not listed here is browsable and read/writable but never scanned or searched — the safe default for "mount a whole drive" (size, privacy). |
| `files_external_mounts_exclude` | `FILES_EXTERNAL_MOUNTS_EXCLUDE` | `""` | Exclude globs per mount: `name=pat1\|pat2;name2=pat3`. Matched against both the mount-relative path and the bare filename during a scan/watch walk; a matching directory is not descended into. Ignored for a mount not in `files_external_mounts_indexed`. |

**Addressing.** A mount-relative path is `mount:<name>/<sub-path>` (or `mount:<name>` alone for
its own root) — the one scheme used everywhere: the platform API's `path` query param,
`PlatformClient.files_*`, and the agent's `storage_*` tools all pass this string straight
through unchanged; `files_routes.py`'s `_target()` helper resolves the prefix to the right
`FileStore` before any tenant-store call. The tenant space itself is addressed exactly as
before (no prefix) — declaring a mount changes nothing about existing paths.

**Store.** Each mount is its own `LocalFileStore(path, tenant_subdir=False)` — a completely
separate root from the tenant tree and from every other mount (constraint #3 needs no new
backend: an external mount is host-filesystem content by definition, so there's no S3
equivalent of "bind a host directory"). `tenant` is still validated on every call (constraint
#1), even though a mount today applies to the one v1 tenant regardless — a per-tenant mount ACL
is SaaS-tier follow-up work, not built here.

**Read-only enforcement.** A `ro` mount (the default) is enforced **server-side** on every
mutating route (`write`, `delete`, `dir`, `move`, `upload`, `entry` delete) — **403** — not just
hidden in the UI; the Files page also sets a page-level `read_only` flag (`files_page`'s
response) so the shell hides Upload/external-drop for it (`BrowserView.tsx`), a courtesy on top
of the real enforcement. Per-item `movable`/`deletable` follow the same rule: `false` for
everything under a `ro` mount, matching the tenant space's rules for an `rw` one (files
movable, both files and directories deletable).

**Containment.** `normalize_rel` rejects a literal `..` in the request path as always; a
symlink *inside* a mount root pointing outward is caught by `LocalFileStore`'s own resolve +
containment check (`PathEscapeError` → 400, see above) — the same protection the tenant space
has always had, now exercised against content the core doesn't manage and an operator's real
filesystem can plausibly contain (a symlinked subtree in a media library, say).

**Moves stay within one store.** `POST /move` refuses a `src`/`dst` pair that crosses between
the tenant space and a mount, or between two mounts (400) — copying bytes across independent
filesystem roots is a materially different operation from a rename, and out of scope for v1;
mounts are isolated compartments, the same way a module's own top-level subtree already is via
`locked_prefixes`.

**Unknown mount name** → **404** before any filesystem call, on every route.

**Indexing** is opt-in per mount (`files_external_mounts_indexed`) and namespaced with the
`mount:<name>/` prefix in the same `core_files` table the tenant tree uses — an indexed row is
directly addressable with the same string a search hit returns. `purge_stale` and the new
`remove_prefix` (`file_index.py`) are prefix-scoped, so re-scanning one mount (or the tenant
tree) can never delete another's rows. At startup every declared mount's `mount:<name>/`
namespace is purged, then re-scanned if indexed — this also cleans up a mount whose indexing
was just turned off; a mount removed from the config **entirely** is the one case left
un-purged (its rows are simply unreachable — the name 404s — not a leak; see
`epicurus_core_app/mounts.py`).

## Data model

Per-tenant scoping (constraint #1): the local backend writes `<root>/<tenant>/…`; the S3 backend
uses a `{tenant}-files` bucket. The backend (the filesystem or the object bucket) *is* the store
for bytes. On top of it the core owns a **unified file index** (ADR-0063) — a tenant-scoped catalogue
of the file-space tree, populated by the startup scan and kept current by the watcher — that backs
`GET /platform/v1/files/page` and `…/search` (the storage module's objects are merged in at request
time, not stored in this index).

The scan's **purge** half is guarded by the mass de-index fuse (#848). Deletions are weighed
per `(tenant, namespace)` — the tenant tree and each mount separately, on the same scoping the
purge itself uses — and refused when the store reads empty against a populated index, or when
the purge would take at least `FILES_SCAN_FUSE_MAX_DELETE_RATIO` of that namespace's rows and at
least `FILES_SCAN_FUSE_MIN_DELETIONS` of them. A refused scan still **upserts** what it saw (adds
are not destructive); only the deletions are withheld, so a stale mount leaves a *stale* index
rather than an emptied one. The refusal is logged at `ERROR`, exported as
`epicurus_core_file_scan_fuse_tripped` / `epicurus_core_file_scan_fuse_trips_total`
(labelled `tenant` + `namespace`), and readable at `GET /platform/v1/files/scan-status`; the next
scan that purges normally re-arms it.

## Dependencies

Local backend: the filesystem (the core mounts the shared `/data` volume). S3 backend: `aioboto3`
against a MinIO/S3 endpoint. The Files page / read / move / download **merge with or fall back to**
the **storage module** for object entries (chat uploads, agent-written files), proxied over the
internal network. External mounts (#731): the filesystem the operator bound via the compose
overlay — no other service dependency. Uses **no AI**.

## Phase plan (ADR-0052 → ADR-0065)

- **Phase 1 (done):** the `FileStore` abstraction, the `/platform/v1/files/*` contract,
  `PlatformClient.files_*`, and core-side provisioning. Additive — the modules were unchanged.
- **Phase 2 (done, ADR-0063):** the core mounts + provisions the shared volume, owns a unified
  file index (startup scan + watcher), and the Files browser / read / download move to the core
  (`/platform/v1/files/{page,search,download}`); the storage module reads the file space through
  this API and contributes its objects (read/move/download fall back to it).
- **Phase 3 (done, #356/ADR-0064) + read-path tail (done, #346/ADR-0070):** the **knowledge**
  module is now a full **consumer** of the file API. Phase 3 routed its writes through
  `PlatformClient.files_*` (the editor save, the file-tree CRUD, the agent's approved suggestions;
  a vault path maps to the core path `knowledge/<rel>`) and dropped the mount to read-only; the
  tail (#346/ADR-0070) routes its **reads** — the incremental indexer, `read_doc`/`list_docs`,
  attachments, the resolver, the review diff, the agent read tools — through the same API behind a
  `VaultReader` seam, so knowledge **holds no `/data` mount** in normal mode. Watch mode (#232) is
  the lone exception: its inotify watcher reads a disk mount, re-added via a compose override.
- **Phase 4 (done, #357/ADR-0065):** the **notes** module is now the **third write-consumer** of
  the file API — after storage (Phase 2) and knowledge (Phase 3). Its `.md` mirror (`write` /
  `delete` / startup `backfill`) routes through `PlatformClient.files_*` at core path
  `notes/<rel>`, and notes **drops its `/data` mount entirely** (it reads nothing from disk — the
  indexer and editor read Postgres). Postgres stays the source of truth; the mirror is write-only
  output. The tenant-root chown the old `files-init` one-shot did is now folded into the **core
  image's entrypoint** (#421/ADR-0069): the container starts as root, chowns `/data/<tenant>` only,
  then drops to uid 10001 — so the file space is fully core-owned with no init container.

## Run & extend

The store is constructed in `create_app()` via `build_file_store(...)` from the `FILES_*` settings
and mounted by `create_files_router` (`epicurus_core_app/files_routes.py`); the core also mounts the
shared `/data` volume, provisions the tenant root, and starts the file index (startup scan + the
`FILES_WATCH` watcher). A new backend implements `FileStore` and is selected in `build_file_store`.
When adding a module-facing endpoint, extend the router and the matching `PlatformClient.files_*`
method together so the contract stays symmetric (operator-UI-facing routes — `page`, `download`,
`upload`, `entry` delete — deliberately have no client method; modules never call them). The two
operator mutation doors (`upload`, `entry` delete) carry the #479 ownership guard their module
twins (`write`, `DELETE …/files`) must not — modules write and delete inside their own subtrees.

External mounts (#731) are parsed and constructed in `epicurus_core_app/mounts.py`
(`parse_mount_specs` → `build_mounts`) from the `FILES_EXTERNAL_MOUNTS*` settings, then passed
into `create_files_router(..., mounts=...)`; `files_routes.py`'s `_target()` is the one place
that resolves a `mount:<name>/…` path, so every handler stays mount-aware by resolving through
it rather than each reimplementing the prefix check. Adding a route that touches the file space
means calling `_target()` first, same as the existing ones.
