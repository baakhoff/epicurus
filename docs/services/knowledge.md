# knowledge — Obsidian-vault RAG + platform self-documentation

**`epicurus-knowledge`** is a sidecar module that indexes two markdown sources
for retrieval-augmented generation, fully incrementally:

1. **Operator vault** — an Obsidian markdown vault the operator mounts at `/vault`.
2. **Platform docs** (self-documentation) — the `docs/` tree bundled into the image
   at `/docs`; available with **no operator setup** in any deploy.

Chunks are embedded **through the core** (no model key lives here) and stored in
tenant-scoped Qdrant collections. Host port **8085**.

## The contract it exposes

### MCP tools (agent-facing)

| Tool | Inputs | Returns |
| --- | --- | --- |
| `knowledge_search(query, k=5)` | `query`: search phrase; `k`: max results | List of `{note_path, heading, text, score}` ordered by relevance across **both** vault and docs. |
| `knowledge_reindex()` | — | `{indexed, deleted, unchanged}` counts summed over both sources. |

`knowledge_search` merges results from the vault (`<tenant>__knowledge`) and the
platform-docs (`<tenant>__docs`) collections, re-ranked by cosine similarity
score, so the agent sees the most relevant content regardless of source.
Platform-docs results have a `note_path` prefixed with `docs/`
(e.g. `docs/services/knowledge.md`) so the agent can cite the source page.

### Events (NATS)

Emits **`<tenant>.knowledge.index.completed`** after each incremental index run.

### Web UI (manifest, ADR-0007 Tier 1)

| Panel | What it shows / does |
| --- | --- |
| **Status** | `note_count` (vault notes) · `doc_count` (platform-docs pages) · `last_indexed_at`. Polled from `GET /status` via the core's `GET /platform/v1/modules/knowledge/status` proxy. |
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

### HTTP

| Endpoint | Description |
| --- | --- |
| `GET /health` | Liveness probe. |
| `GET /metrics` | Prometheus metrics. |
| `GET /manifest` | Module manifest (tools, events, UI declaration, **`pages`**). |
| `GET /status` | Live index stats: `{note_count, doc_count, last_indexed_at}`. Proxied by the core at `GET /platform/v1/modules/knowledge/status`. |
| `GET /pages/{page_id}` | Editor document list `{title, docs:[{id, title, path}]}` (page id `vault`). Proxied at `GET /platform/v1/modules/knowledge/pages/{page_id}`. |
| `GET /pages/{page_id}/doc?path=<rel>` | One document's content `{path, title, content}`. `path` is vault-relative and strictly confined (no traversal, `.md` only). |
| `PUT /pages/{page_id}/doc?path=<rel>` | Save a document `{content}` → `{path, indexed, chunk_count}`; writes the file then re-indexes it. The write is the source of truth — a failed re-index returns `indexed: false`, never losing the edit. |
| `GET /mcp` (streamable-HTTP) | MCP tool surface (served by FastMCP). |

## How search works

1. The agent calls `knowledge_search(query, k)`.
2. The module concurrently searches `<tenant>__knowledge` (vault) and `<tenant>__docs`
   (platform docs), each embedding `query` via the core's platform API.
3. Results from both collections are merged and re-ranked by descending cosine score.
4. The top-k chunks are returned with `note_path` and `heading` so the agent can cite
   the source.

## How indexing works

The same incremental logic applies to both sources:

1. **Hash** the file (sha-256) and compare with the DB record — skip unchanged files.
2. **Chunk** new/changed files heading-aware, hard-splitting at paragraph boundaries past
   `CHUNK_MAX_CHARS`.
3. **Embed** each chunk via the core's [`PlatformClient`](../reference/platform-client.md)
   (`POST /platform/v1/embed`) — **no model key lives in this module**.
4. **Upsert** the vectors into Qdrant (deterministic UUID5 point ids) and record the
   file's hash/mtime/chunk-count in Postgres.

Deleted files are purged from both stores on the next index run.

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
- **Qdrant `<tenant>__knowledge`** — vault chunk embeddings (cosine), one collection per tenant.
- **Qdrant `<tenant>__docs`** — platform-docs chunk embeddings (cosine), one collection per tenant.

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
| `db.py` | `knowledge_notes` ledger (`NoteIndex`) + `knowledge_doc_index` ledger (`DocIndex`). |
| `indexer.py` | Diff + embed + upsert + semantic search (`KnowledgeIndexer`, parameterised by source); `index_path` re-indexes a single file for the editor save. |
| `service.py` | MCP tools (`knowledge_search`, `knowledge_reindex`) + manifest UI + the `editor` page spec. |
| `pages.py` | The `editor` page surface (#130): document list, read, and save (with vault-path safety + re-index). |
| `app.py` | Lifespan, `GET /status` endpoint, the `/pages/*` router, initial index of both sources on startup. |
| `settings.py` | `KnowledgeSettings` (adds `vault_path`, `docs_path`, Qdrant, DB, platform URL). |
