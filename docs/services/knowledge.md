# knowledge — Obsidian-vault RAG + platform self-documentation

**`epicurus-knowledge`** is a sidecar module that indexes three markdown sources
for retrieval-augmented generation, fully incrementally:

1. **Operator vault** — an Obsidian markdown vault the operator mounts at `/vault`.
2. **Platform docs** (self-documentation) — the `docs/` tree bundled into the image
   at `/docs`; available with **no operator setup** in any deploy.
3. **Module docs** — usage documentation contributed by each enabled module via a
   `docs_url` endpoint, auto-indexed on startup; disabled modules have their docs
   purged automatically (#215).

Chunks are embedded **through the core** (no model key lives here) and stored in
tenant-scoped Qdrant collections. Host port **8085**.

## The contract it exposes

### MCP tools (agent-facing)

| Tool | Inputs | Returns |
| --- | --- | --- |
| `knowledge_search(query, k=5)` | `query`: search phrase; `k`: max results | A `ToolEnvelope`: the top-`k` matching chunks as readable text **plus** one entity-reference chip per cited document (ADR-0019). |
| `knowledge_reindex()` | — | `{indexed, deleted, unchanged}` counts summed over all three sources (vault + platform docs + module docs). |

`knowledge_search` merges results from the vault (`<tenant>__knowledge`) and the
platform-docs (`<tenant>__docs`) collections, re-ranked by cosine similarity score, so the
agent sees the most relevant content regardless of source. It returns a **`ToolEnvelope`**
(ADR-0019): the chunk text (so the agent can quote and reason over it) plus one
**entity-reference chip per distinct cited document** — hovering a chip shows a hover-card
and clicking a vault note opens it in the Knowledge page (see *Hover-cards* below).
Platform-docs citations are shown with a `docs/` path prefix so the agent can tell them
apart from vault notes.

### Events (NATS)

Emits **`<tenant>.knowledge.index.completed`** after each incremental index run.

### Web UI (manifest, ADR-0007 Tier 1)

| Panel | What it shows / does |
| --- | --- |
| **Status** | `note_count` (vault notes) · `doc_count` (platform-docs pages) · `module_doc_count` (module-contributed docs) · `last_indexed_at` · `index_phase` / `index_attempts` (background-index progress, #230). Polled from `GET /status` via the core's `GET /platform/v1/modules/knowledge/status` proxy. |
| **Settings** | Vault path (`VAULT_PATH`) — editable in the shell. |
| **Actions** | **Re-index** — triggers `knowledge_reindex` (both sources) through the core. |

No module code runs in the shell; all data flows through the core.

### Knowledge page (`editor` archetype, ADR-0018)

The module contributes a **Knowledge** left-nav page — an Obsidian-style browse-and-edit
view over the vault, declared as a `pages` entry `{id: "vault", archetype: "editor"}`.
The **core renders** the editor from its bounded vocabulary (a document list, a markdown
source/preview editor, a save button); the module ships **no markup** and only supplies
data over three endpoints the core proxies (`GET /pages/{id}`, `GET/PUT /pages/{id}/doc`).

Saving a document writes it back to the vault and **re-indexes just that file** into
`<tenant>__knowledge`, so an edit made in the shell is immediately retrievable by the
agent (the vault is agent-retrievable by default — contrast a future Notes module). The
editor component is **core-owned and shared**; Notes reuses it. The bundled platform docs
are *not* exposed as an editor page (they are read-only, image-bundled self-documentation).

The vault must be mounted **read-write** for saving to work (see Configuration); the
default empty named volume is writable, and an operator binding their Obsidian vault should
mount it writable by the container user (uid 10001).

### Attachments (chat-context source, #137)

A vault document can be **attached to a chat turn** as explicit context, beyond default
retrieval. The module declares `attachable: true` and supplies data only — the core's
attach menu renders the picker and the agent's `AttachmentExpander` injects the resolved
text into the turn:

- `GET /attachments` — the picker: every vault document, as `{ref_id, kind, title}`.
- `GET /attachments/{ref_id}` — the resolve: `{title, path, text}` for one document.

Only the operator's **vault** is attachable; the bundled platform docs reach the agent
through retrieval, not the picker. A `ref_id` is **opaque** — base64url of the document's
`source:path` — so it round-trips as a single URL path segment regardless of folder depth
(see `refs.py`).

### Hover-cards for cited documents (#143)

A `knowledge_search` citation renders in chat as an entity-reference chip. Hovering it asks
the core for a hover-card, proxied to `GET /resolve/knowledge/{ref_id}`. The module declares
`resolver: true` and supplies the data; the core renders the uniform `HoverCard`:

- **vault note** → `{title, description (a preview), details: [Path, Tags, Last indexed], href}`
  where `href` deep-links into the Knowledge page (`/m/knowledge/vault?doc=…`) so a click
  opens the document to read or edit.
- **platform doc** → the same card with a `docs/`-prefixed path and **no** `href` — the
  read-only platform docs have no editor page.

`Tags` are read best-effort from a note's YAML frontmatter; `Last indexed` comes from the
index ledger. The web renders an in-app `href` as a same-tab router link (the shared
`CardLink`), so opening a cited note never reloads the shell.

### HTTP

| Endpoint | Description |
| --- | --- |
| `GET /health` | Liveness probe. |
| `GET /metrics` | Prometheus metrics. |
| `GET /manifest` | Module manifest (tools, events, UI declaration, **`pages`**, **`attachable`**, **`resolver`**). |
| `GET /status` | Live index stats: `{note_count, doc_count, module_doc_count, last_indexed_at, index_phase, index_attempts}`. `index_phase` ∈ `pending`/`indexing`/`ready`/`retrying`/`error` (#230). Proxied by the core at `GET /platform/v1/modules/knowledge/status`. |
| `GET /pages/{page_id}` | Editor document list `{title, docs:[{id, title, path}]}` (page id `vault`). Proxied at `GET /platform/v1/modules/knowledge/pages/{page_id}`. |
| `GET /pages/{page_id}/doc?path=<rel>` | One document's content `{path, title, content}`. `path` is vault-relative and strictly confined (no traversal, `.md` only). |
| `PUT /pages/{page_id}/doc?path=<rel>` | Save a document `{content}` → `{path, indexed, chunk_count}`; writes the file then re-indexes it. The write is the source of truth — a failed re-index returns `indexed: false`, never losing the edit. |
| `GET /attachments` | Attachment picker: every vault doc as `{ref_id, kind, title}` (#137). Proxied at `GET /platform/v1/modules/knowledge/attachments`. |
| `GET /attachments/{ref_id}` | Attachment resolve: `{title, path, text}` for one vault doc; the core injects it into the turn. `ref_id` is the opaque base64url id from the picker. |
| `GET /resolve/{kind}/{ref_id}` | Hover-card resolver (#143): a cited doc → a `HoverCard`. `kind` is `knowledge`. Proxied at `GET /platform/v1/modules/knowledge/resolve/{kind}/{ref_id}`. |
| `GET /module-docs` | Module docs endpoint — `{"documents": [{"path": "…", "content": "…"}]}`; the knowledge indexer fetches this via the core proxy (#215). Not `/docs` (FastAPI Swagger UI). |
| `GET /mcp` (streamable-HTTP) | MCP tool surface (served by FastMCP). |

## How search works

1. The agent calls `knowledge_search(query, k)`.
2. The module concurrently searches `<tenant>__knowledge` (vault) and `<tenant>__docs`
   (platform docs), each embedding `query` via the core's platform API.
3. Results from both collections are merged and re-ranked by descending cosine score.
4. The top-`k` chunks are returned as a `ToolEnvelope` — readable text for the agent plus
   one entity-reference chip per distinct cited document (a vault note, or a
   `docs/`-prefixed platform doc), each carrying an opaque `ref_id` the hover-card resolver
   round-trips (#143).

## How indexing works

The same incremental logic applies to the vault and platform-docs sources:

1. **Hash** the file (sha-256) and compare with the DB record — skip unchanged files.
2. **Chunk** new/changed files heading-aware, hard-splitting at paragraph boundaries past
   `CHUNK_MAX_CHARS`.
3. **Embed** chunks in **batches across files** via the core's
   [`PlatformClient`](../reference/platform-client.md) (`POST /platform/v1/embed`) — the
   indexer accumulates chunks until `EMBED_BATCH_SIZE` are queued, then embeds them in one
   round-trip, so the bundled docs index in a handful of calls rather than one per file
   (#230). **No model key lives in this module.**
4. **Upsert** the vectors into Qdrant (deterministic UUID5 point ids) and record each
   file's hash/mtime/chunk-count in Postgres only after its vectors land, so an interrupted
   run leaves the ledger consistent.

Deleted files are purged from both stores on the next index run.

### Resilient startup (#230)

The initial index runs as a **background task**: the service serves `GET /health`
immediately rather than blocking until the (potentially multi-minute) first index over a
real vault completes, so the healthcheck never flaps and orchestration won't trip restart
loops. The background runner (`runner.IndexRunner`) **retries with capped exponential
backoff**, so a cold `compose up` that starts knowledge before core-app / qdrant are
reachable still ends with a populated index without any manual restart. `GET /status`
exposes the live `index_phase` and `index_attempts`; counts climb as the run progresses.

**Module docs** use a variant of the same logic (`ModuleDocsIndexer`): each enabled module's
`docs_url` is fetched via the core proxy, diffed by SHA-256 content hash (HTTP sources have no
reliable mtime), and upserted into `<tenant>__docs` under a `module/<name>/` path prefix.
Modules that are disabled or removed have their entries purged. A separate UUID namespace
keeps module-doc point IDs from colliding with platform-doc IDs in the shared collection.
Tracked in `knowledge_module_docs` (see *Data model*).

## Self-documentation (platform docs source)

The platform docs (`docs/` tree) are **bundled into the container image** via
`COPY docs/ /docs` in the Dockerfile. On startup the service indexes `/docs`
into `<tenant>__docs` automatically — no operator configuration required.

To use a live docs tree instead (e.g. during development):

```bash
# In .env
DOCS_PATH=/absolute/path/to/epicurus/docs
# In compose.override.yaml
services:
  knowledge:
    volumes:
      - ${DOCS_PATH}:/docs:ro
```

## Module-contributed docs (#215)

On startup (and on every `knowledge_reindex` call) the service fetches the module list from
the core (`GET /platform/v1/modules`), identifies every **enabled, non-removed** module that
declares a `docs_url` in its manifest, and retrieves their documents via
`GET /platform/v1/modules/{name}/docs`. Each document is diffed by SHA-256 hash, embedded
via the core, and upserted into `<tenant>__docs` under the `module/<name>/` path prefix. A
module that goes offline during a run is skipped gracefully; other modules still index. Docs
for modules that are **disabled or removed** are purged from the collection automatically.

This means a fresh install ships with both platform docs *and* every module's usage docs
already indexed — no operator action required. The `knowledge_search` tool returns module docs
alongside platform docs seamlessly because they share the `<tenant>__docs` collection.

To contribute docs from a new module, declare `docs_url="/module-docs"` in its `EpicurusModule(...)`
constructor and serve the JSON shape `{"documents": [{"path": "…", "content": "…"}]}` at
that path. The platform-client helper (`PlatformClient.get_module_docs()`) lets other
platform services read these docs if needed.

## Configuration

`KnowledgeSettings` extends [`CoreSettings`](../reference/config.md):

| Env var | Default | Meaning |
| --- | --- | --- |
| `VAULT_PATH` | `/vault` | In-container path of the Obsidian vault. |
| `DOCS_PATH` | `/docs` | In-container path of the platform docs (bundled in image). |
| `PLATFORM_URL` | `http://core-app:8080` | The core's base URL (for embeddings via the platform API). |
| `QDRANT_URL` | `http://qdrant:6333` | Vector index. |
| `DATABASE_URL` | `postgresql+asyncpg://…/epicurus` | File hash/mtime tracking. |
| `CHUNK_MAX_CHARS` | `2000` | Max chars per chunk before a hard split. |
| `EMBED_BATCH_SIZE` | `64` | Chunk texts embedded per `/embed` round-trip — the indexer flushes a batch once this many chunks are queued (#230). |
| `INDEX_RETRY_MAX_ATTEMPTS` | `30` | Background-index retry cap before giving up (#230). |
| `INDEX_RETRY_BASE_DELAY_SECONDS` | `1.0` | First retry backoff; doubles each attempt. |
| `INDEX_RETRY_MAX_DELAY_SECONDS` | `30.0` | Upper bound on the retry backoff. |

The vault is bound to `/vault` **read-write** via `KNOWLEDGE_HOST_VAULT`, which
defaults to an **empty named volume** (point it at your vault to index real notes).
Read-write so the Knowledge editor page can save edits back to the vault (#130); mount a
host directory the container user (uid 10001) can write. The platform docs at `/docs` are
always present — bundled at image build time, and are not editable from the shell.

## Data model

- **Postgres `knowledge_notes`** — vault incremental-index ledger: `id`, `tenant`,
  `note_path`, `mtime_ns` (BigInteger — nanosecond mtimes overflow int32), `content_hash`
  (sha-256), `chunk_count`, `indexed_at`; unique on `(tenant, note_path)`.
- **Postgres `knowledge_doc_index`** — identical structure for platform-docs tracking;
  separate table so vault and docs paths can't collide.
- **Postgres `knowledge_module_docs`** — module-docs ledger (#215): `id`, `tenant`,
  `module_name`, `doc_path`, `content_hash` (SHA-256), `chunk_count`, `indexed_at`;
  unique on `(tenant, module_name, doc_path)`. Records are purged when a module is
  disabled or removed.
- **Qdrant `<tenant>__knowledge`** — vault chunk embeddings (cosine), one collection per tenant.
- **Qdrant `<tenant>__docs`** — platform-docs + module-docs chunk embeddings (cosine), one
  collection per tenant. Module-doc points use a distinct UUID namespace from platform-doc
  points to avoid ID collisions in the shared collection.

Each Qdrant point payload: `{note_path, chunk_index, heading, text}`.

## Dependencies

core-app (embeddings + status proxy via the platform API) · Qdrant (vectors) · Postgres
(file tracking) · NATS (the index-completed event) · the mounted vault · bundled docs.

## Run & extend

```bash
# With your Obsidian vault (docs auto-indexed from the image):
KNOWLEDGE_HOST_VAULT=/path/to/your/vault docker compose up -d knowledge

# Without a vault (only platform docs are indexed):
docker compose up -d knowledge
```

Package `epicurus_knowledge`:

| Module | Responsibility |
| --- | --- |
| `chunker.py` | Heading-aware markdown splitter. |
| `db.py` | `knowledge_notes` ledger (`NoteIndex`) + `knowledge_doc_index` ledger (`DocIndex`); per-path `indexed_at` powers the hover-card's *Last indexed*. |
| `indexer.py` | Diff + batched embed + upsert + semantic search (`KnowledgeIndexer`, parameterised by source); accumulates chunks across files and flushes per `EMBED_BATCH_SIZE` (#230); `index_path` re-indexes a single file for the editor save. |
| `runner.py` | `IndexRunner` (#230): runs every source indexer in the background with retry/backoff and exposes `IndexState` for `GET /status`. |
| `service.py` | MCP tools (`knowledge_search` → entity-ref chips, `knowledge_reindex`) + manifest UI + the `editor` page spec. |
| `pages.py` | The `editor` page surface (#130): document list, read, and save (with vault-path safety + re-index). |
| `refs.py` | Opaque document refs (base64url `source:path`) + shared `.md` vault path-safety + vault walk. |
| `attachments.py` | The attachment source (#137): vault-doc picker + resolve (`VaultAttachments`). |
| `resolver.py` | The hover-card resolver (#143): a cited vault note or platform doc → a `HoverCard` (`KnowledgeResolver`). |
| `module_docs.py` | `ModuleDocLedger` (Postgres tracking for module-contributed docs) + `ModuleDocsIndexer` (HTTP-based diff/embed/upsert for module docs, #215). |
| `app.py` | Lifespan, `GET /status`, the `/pages/*` + `/attachments/*` + `/resolve/*` + `/module-docs` routers; launches the background `IndexRunner` (#230) so startup never blocks on the first index. |
| `settings.py` | `KnowledgeSettings` (adds `vault_path`, `docs_path`, Qdrant, DB, platform URL). |
