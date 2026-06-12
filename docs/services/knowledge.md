# knowledge — Obsidian-vault RAG

**`epicurus-knowledge`** is a sidecar module that indexes an **Obsidian** markdown vault
for retrieval-augmented generation: it chunks notes, embeds them **through the core**, and
maintains a tenant-scoped Qdrant collection — fully incrementally. Host port **8085**.

## The contract it exposes

### MCP tools (agent-facing)

| Tool | Inputs | Returns |
| --- | --- | --- |
| `knowledge_search(query, k=5)` | `query`: search phrase; `k`: max results | List of `{note_path, heading, text, score}` ordered by relevance. |
| `knowledge_reindex()` | — | `{indexed, deleted, unchanged}` counts. |

The agent calls `knowledge_search` to ground answers in vault content and cite the source
note. It calls `knowledge_reindex` to refresh the index after notes have changed.

### Events (NATS)

Emits **`<tenant>.knowledge.index.completed`** after each incremental index run.

### Web UI (manifest, ADR-0007 Tier 1)

| Panel | What it shows / does |
| --- | --- |
| **Status** | `note_count` (notes indexed) · `last_indexed_at` (ISO-8601 timestamp). Polled from `GET /status` via the core's `GET /platform/v1/modules/knowledge/status` proxy. |
| **Settings** | Vault path (`VAULT_PATH`) — editable in the shell. |
| **Actions** | **Re-index vault** — triggers `knowledge_reindex` through the core. |

No module code runs in the shell; all data flows through the core.

### HTTP

| Endpoint | Description |
| --- | --- |
| `GET /health` | Liveness probe. |
| `GET /metrics` | Prometheus metrics. |
| `GET /manifest` | Module manifest (tools, events, UI declaration). |
| `GET /status` | Live index stats: `{note_count, last_indexed_at}`. Proxied by the core at `GET /platform/v1/modules/knowledge/status`. |
| `GET /mcp` (streamable-HTTP) | MCP tool surface (served by FastMCP). |

## How search works

1. The agent calls `knowledge_search(query, k)`.
2. The module embeds `query` via the core's platform API (`POST /platform/v1/embed`).
3. It queries the tenant's Qdrant collection for the top-k nearest vectors.
4. Returns the matching chunks with `note_path` and `heading` so the agent can cite the
   source note in its reply.

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
- **Qdrant `<tenant>__knowledge`** — chunk embeddings (cosine), one collection per tenant.
  Each point payload: `{note_path, chunk_index, heading, text}`.

## Dependencies

core-app (embeddings + status proxy via the platform API) · Qdrant (vectors) · Postgres
(note tracking) · NATS (the index-completed event) · the mounted vault.

## Run & extend

```bash
KNOWLEDGE_HOST_VAULT=/path/to/your/vault docker compose up -d knowledge
```

Package `epicurus_knowledge`:

| Module | Responsibility |
| --- | --- |
| `chunker.py` | Heading-aware markdown splitter. |
| `db.py` | `knowledge_notes` Postgres ledger (`NoteIndex`). |
| `indexer.py` | Diff + embed + upsert run + semantic search (`KnowledgeIndexer`). |
| `service.py` | MCP tools (`knowledge_search`, `knowledge_reindex`) + manifest UI. |
| `app.py` | Lifespan, `GET /status` endpoint, initial index on startup. |
| `settings.py` | `KnowledgeSettings`. |
