# knowledge — Obsidian-vault RAG

**`epicurus-knowledge`** is a sidecar module that indexes an **Obsidian** markdown vault
for retrieval-augmented generation: it chunks notes, embeds them **through the core**, and
maintains a tenant-scoped Qdrant collection — fully incrementally. Host port **8085**.

> Today the module **ingests and maintains the index**. The retrieval tool that lets the
> agent answer from the vault (`knowledge_search`) lands with its own card (#69); this
> page covers the ingestion half that exists now.

## The contract it exposes

### MCP tools (agent-facing)

| Tool | Purpose |
| --- | --- |
| `knowledge_reindex()` | Re-walk the vault and update the index; returns counts (`indexed` / `deleted` / `unchanged`). |

### Events (NATS)

Emits **`<tenant>.knowledge.index.completed`** after each incremental index run.

### Web UI (manifest)

A **Reindex** action and a vault-path config field, auto-rendered by the shell.

### HTTP

`GET /health` · `GET /metrics` · `GET /manifest`.

## How indexing works

For each `.md` note under the vault:

1. **Hash** the file (sha-256) and compare with the DB record — skip unchanged notes.
2. **Chunk** new/changed notes heading-aware, hard-splitting at paragraph boundaries past
   `CHUNK_MAX_CHARS`.
3. **Embed** each chunk via the core's [`PlatformClient`](../reference/platform-client.md)
   (`POST /platform/v1/embed`) — **no model key lives in this module**.
4. **Upsert** the vectors into Qdrant (deterministic UUID5 point ids) and record the
   note's hash/mtime/chunk-count in Postgres.

Deleted notes are purged from both stores. The result is an incremental index: editing one
note re-embeds only that note.

## Configuration

`KnowledgeSettings` extends [`CoreSettings`](../reference/config.md):

| Env var | Default | Meaning |
| --- | --- | --- |
| `VAULT_PATH` | `/vault` | In-container path of the Obsidian vault. |
| `PLATFORM_URL` | `http://core-app:8080` | The core's base URL (for embeddings via the platform API). |
| `QDRANT_URL` | `http://qdrant:6333` | Vector index. |
| `DATABASE_URL` | `postgresql+asyncpg://…/epicurus` | Note hash/mtime tracking. |
| `CHUNK_MAX_CHARS` | `2000` | Max chars per chunk before a hard split. |

In the stack, the vault is bound to `/vault` **read-only** via `KNOWLEDGE_HOST_VAULT`,
which defaults to an **empty named volume** (point it at your vault to index real notes).

## Data model

- **Postgres `knowledge_notes`** — the incremental-index ledger: `id`, `tenant`,
  `note_path`, `mtime_ns` (BigInteger — nanosecond mtimes overflow int32), `content_hash`
  (sha-256), `chunk_count`, `indexed_at`; unique on `(tenant, note_path)`.
- **Qdrant `<tenant>__knowledge`** — chunk embeddings (768-dim, cosine), one collection
  per tenant.

## Dependencies

core-app (embeddings via the platform API) · Qdrant (vectors) · Postgres (note tracking)
· NATS (the index-completed event) · the mounted vault.

## Run & extend

```bash
KNOWLEDGE_HOST_VAULT=/path/to/your/vault docker compose up -d knowledge
```

Package `epicurus_knowledge`: `chunker.py` (heading-aware splitter), `db.py`
(`knowledge_notes`), `indexer.py` (the diff + embed + upsert run), `service.py` (the MCP
tool + manifest UI), `app.py` (lifespan + initial index on startup).
