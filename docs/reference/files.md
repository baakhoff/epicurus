# File space (`epicurus_core.files`) — the core-owned, swappable file store

**What it is.** The per-tenant user file space, owned by the **core** behind a swappable
backend (ADR-0052). One `FileStore` interface, tenant-scoped on every call (constraint #1),
with a local-filesystem backend (self-host) and an S3/MinIO backend (SaaS) behind the same
contract — so no module hardcodes where files live (constraint #3). Modules read and write the
space through the platform API (`/platform/v1/files/*`) via `PlatformClient`, instead of mounting
the shared `/data` volume and doing their own I/O.

> **Phased rollout.** This page documents **Phase 1**: the abstraction, the wire contract, and
> the consumer client. The modules (storage / knowledge / notes) still own their own subtrees
> today; migrating them onto this API — and serving the Files browser from the core — is staged
> follow-up work (see the phase plan below).

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
| `GET /platform/v1/files/list?path=&tenant_id=` | `{entries: [FileEntry]}` — children of `path` (empty = root). |
| `GET /platform/v1/files/read?path=&tenant_id=` | `{path, name, content}` — UTF-8 text. **404** missing, **413** > 256 KB, **415** binary, **400** traversal. |
| `GET /platform/v1/files/stat?path=&tenant_id=` | A `FileEntry`, or **404**. |
| `PUT /platform/v1/files/write?path=&tenant_id=` (body `{content}`) | Write UTF-8 text → `FileEntry`. **400** writing the root. |
| `DELETE /platform/v1/files?path=&tenant_id=` | `{deleted}` — a file or a whole tree. **400** deleting the root. |
| `POST /platform/v1/files/dir?path=&tenant_id=` | Create a directory → `FileEntry`. |

### `PlatformClient` methods

`files_list(path="")`, `files_read(path)`, `files_write(path, content)`, `files_stat(path)`,
`files_delete(path)`, `files_make_dir(path)` — the typed module-side consumer of the endpoints
above (`files_stat` returns `None` on 404).

## Configuration (core-app)

| Setting | Env | Default | Meaning |
| --- | --- | --- | --- |
| `files_backend` | `FILES_BACKEND` | `local` | `local` (filesystem) or `s3` (MinIO/S3). |
| `files_root` | `FILES_ROOT` | `/data` | Local-backend base; the tenant tree is `FILES_ROOT/<tenant>`. |
| `files_s3_url` | `FILES_S3_URL` | `http://minio:9000` | S3 endpoint (when `files_backend=s3`). |
| `files_s3_access_key` / `files_s3_secret_key` | `FILES_S3_ACCESS_KEY` / `FILES_S3_SECRET_KEY` | `epicurus` / `epicurus-dev` | S3 credentials (dev defaults; OpenBao later). |

## Data model

Per-tenant scoping (constraint #1): the local backend writes `<root>/<tenant>/…`; the S3 backend
uses a `{tenant}-files` bucket. There is **no new database table** — the backend (the filesystem
or the object bucket) *is* the store. A core-owned unified file *index* is Phase 2.

## Dependencies

Local backend: the filesystem. S3 backend: `aioboto3` against a MinIO/S3 endpoint. Uses **no AI**.

## Phase plan (ADR-0052)

- **Phase 1 (this PR):** the `FileStore` abstraction, the `/platform/v1/files/*` contract,
  `PlatformClient.files_*`, and core-side provisioning. Additive — the modules are unchanged.
- **Phase 2:** the core mounts + provisions the shared volume; the Files browser and
  `/read`/`/download` move to the core; a core-owned unified file index; the storage module reads
  through this API.
- **Phase 3:** knowledge writes through the file API and drops its direct `/data` mount.
- **Phase 4:** notes mirror through the file API and drops its direct mount; `files-init` retires.

## Run & extend

The store is constructed in `create_app()` via `build_file_store(...)` from the `FILES_*` settings
and mounted by `create_files_router` (`epicurus_core_app/files_routes.py`). A new backend
implements `FileStore` and is selected in `build_file_store`. When adding an endpoint, extend the
router and the matching `PlatformClient.files_*` method together so the contract stays symmetric.
