# epicurus-knowledge

Obsidian vault RAG module — incrementally indexes markdown notes into a
tenant-scoped Qdrant collection so the agent can search them by meaning.

## What it does

On startup (and on each `knowledge_reindex` tool call) the service:

1. Walks every `.md` file in the configured vault.
2. Skips notes whose content hash matches the last-indexed value.
3. Chunks changed notes by heading structure, keeping each chunk under
   `CHUNK_MAX_CHARS` characters.
4. Requests embeddings from the core's platform API (the module never holds
   provider credentials — all inference goes through the core).
5. Upserts chunks to Qdrant under a tenant-scoped collection
   (`<tenant>__knowledge`).
6. Removes Qdrant points for notes deleted from the vault.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `VAULT_PATH` | `/vault` | Absolute path to the Obsidian vault inside the container |
| `DATABASE_URL` | `postgresql+asyncpg://…` | Async Postgres DSN for the note tracking index |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant endpoint |
| `PLATFORM_URL` | `http://localhost:8080` | Core service base URL (platform API) |
| `CHUNK_MAX_CHARS` | `2000` | Maximum characters per chunk before paragraph-splitting |
| `DEFAULT_TENANT_ID` | `local` | Tenant to scope all data under |

## Mounting your vault

In your `.env`, point `KNOWLEDGE_HOST_VAULT` at the directory on the host:

```env
KNOWLEDGE_HOST_VAULT=/path/to/your/obsidian-vault
```

Then include this module's compose fragment in your top-level `compose.yaml`:

```yaml
include:
  - services/knowledge/compose.yaml
```

## MCP tools

| Tool | Description |
|---|---|
| `knowledge_reindex` | Incrementally re-index the vault; returns `{"indexed", "deleted", "unchanged"}` |

The semantic-search tool (`knowledge_search`) ships in the next issue (#69).

## NATS events

| Subject | When |
|---|---|
| `knowledge.index.completed` | After each incremental index run |

## Development

```bash
# Run tests (from repo root)
uv run pytest services/knowledge/

# Type-check
uv run mypy --strict services/knowledge/src

# Lint
uv run ruff check services/knowledge/
```
