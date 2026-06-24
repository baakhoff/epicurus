# notes — author Obsidian-style notes into a private, attach-only collection

**`epicurus-notes`** is a sidecar module for writing notes in the ε editor. A note is
saved to Postgres (the source of truth) and indexed into its **own** tenant-scoped Qdrant
collection, so notes are RAG-ready — but they are **attach-only**: the module exposes
**no agent tool**, so the assistant can read a note *only* when the user attaches it to a
message. This is the line between **Notes** (you author + manually attach) and
[**Knowledge**](knowledge.md) (your vault, agent-retrievable). Host port **8092**.

Unlike knowledge — which indexes an Obsidian vault that lives on disk — notes are authored
in the app, so their content is **externalized state** (Postgres), not local disk
(constraint #2). Embeddings are obtained **through the core** (no model key lives here).

## The contract it exposes

### MCP tools (agent-facing)

**None — by design.** Notes registers no tools, so the agent has no automatic access to a
note. Access is attach-only (see below). The manifest's `tools` list is empty.

### Chat attachments (ADR-0019)

The module is an **attachment source** (`attachable: true`) — the only path from a note
into the agent's context:

| Endpoint | Returns |
| --- | --- |
| `GET /attachments` | The picker: every note as `{ref_id, kind: "note", title}` (newest first). |
| `GET /attachments/{ref_id}` | Resolve: `{title, excerpt}` — the note body the core injects into the turn. |

`ref_id` is the note slug. Both are proxied by the core
(`GET /platform/v1/modules/notes/attachments[/{ref_id}]`); the shell renders the picker.

### Notes page (`editor` archetype, ADR-0018 / ADR-0022 / ADR-0026)

The module contributes a **Notes** left-nav page — declared as a `pages` entry
`{id: "notes", archetype: "editor"}`. The **core renders** the editor (a document list, a
markdown editor that **opens rendered** and **saves on leave / idle / explicit Save** —
not per keystroke, since each save re-embeds (ADR-0042) — and, because the page sets
`can_create`, a **New note** control); the module ships **no markup** and only supplies
data over three endpoints the core proxies. Saving to a new slug **creates** the note;
saving an existing one updates it. Each save re-indexes the note into `<tenant>__notes`.

The shared `EditorView` is **core-owned**; notes reuses it (knowledge is the other user).
The note **title** is derived from the body (its first heading / line), so the
`{content}`-only save contract needs no title field; the **slug** (the editor `path`) is a
Postgres key, not a filesystem path — there is no traversal surface, only slug validation.

The page is **versioned** (`versioned: true`, ADR-0046): every save snapshots the note's
body, and the shell offers a **browse + restore past versions** affordance. A byte-identical
re-save is deduped (no new snapshot), and history is bounded to the newest **50** versions
per note. **Restore is client-side** — the shell fetches a past version and re-saves its
content through the normal save path; the module exposes **no restore endpoint**.

### Events (NATS)

Emits **`<tenant>.notes.saved`** (`{slug}`) after a note is saved and indexed.

### Web UI (manifest, ADR-0007 Tier 1)

| Panel | What it shows / does |
| --- | --- |
| **Status** | `note_count` · `last_updated_at`. Polled from `GET /status` via the core's `GET /platform/v1/modules/notes/status` proxy. |

No settings, no actions, no module code in the shell — Notes has no configurable surface
and no agent tools; all data flows through the core.

### HTTP

| Endpoint | Description |
| --- | --- |
| `GET /health` | Liveness probe. |
| `GET /metrics` | Prometheus metrics. |
| `GET /manifest` | Module manifest (empty `tools`, `attachable: true`, `pages`, UI). |
| `GET /status` | Live stats `{note_count, last_updated_at}`. Proxied at `GET /platform/v1/modules/notes/status`. |
| `GET /pages/{page_id}` | Editor document list `{title, docs:[{id, title, path}], can_create: true}` (page id `notes`). |
| `GET /pages/{page_id}/doc?path=<slug>` | One note's content `{path, title, content}`. |
| `PUT /pages/{page_id}/doc?path=<slug>` | Save (create-on-absent) `{content}` → `{path, indexed, chunk_count}`. The note is the source of truth — a failed re-index returns `indexed: false`, never losing the write. Each save also snapshots the body for version history (ADR-0046). |
| `GET /pages/{page_id}/doc/versions?path=<slug>` | A note's past versions, newest first → `{versions:[{version_id, created_at, title, size}]}` (ADR-0046, capped at 50). |
| `GET /pages/{page_id}/doc/version?path=<slug>&version=<version_id>` | One past version's full content → `{path, version_id, created_at, title, content}`; 404 if it is not that note's version. |
| `GET /attachments` | Attachment picker (see above). |
| `GET /attachments/{ref_id}` | Attachment resolve (see above). |
| `GET /mcp` (streamable-HTTP) | MCP surface (no tools registered). |

## How saving + indexing works

1. The editor `PUT`s a note's full content to its slug.
2. The module derives the **title** from the body and **upserts** the row in Postgres
   (the source of truth) — written first so an edit is never lost.
3. It then **chunks** the body heading-aware (hard-splitting past `CHUNK_MAX_CHARS`),
   **embeds** each chunk via the core's [`PlatformClient`](../reference/platform-client.md)
   (`POST /platform/v1/embed`, **no model key here**), and **upserts** the vectors into
   `<tenant>__notes` (stale vectors for the slug are dropped first).
4. On success it publishes `notes.saved`. If the embed round-trip fails (e.g. the core is
   paused), the save still succeeds with `indexed: false`; the next save retries.

The `<tenant>__notes` collection is written so notes are immediately RAG-ready, but **no
retrieval path queries it today** — attach reads the note body straight from Postgres. The
collection exists so a future, opt-in retrieval feature needs no re-index.

## Configuration

`NotesSettings` extends [`CoreSettings`](../reference/config.md):

| Env var | Default | Meaning |
| --- | --- | --- |
| `PLATFORM_URL` | `http://core-app:8080` | The core's base URL (embeddings via the platform API). |
| `QDRANT_URL` | `http://qdrant:6333` | Vector index. |
| `DATABASE_URL` | `postgresql+asyncpg://…/epicurus` | Note bodies (source of truth). |
| `CHUNK_MAX_CHARS` | `2000` | Max chars per chunk before a hard split. |
| `NOTES_PORT` | `8092` | Host port (loopback-bound by default). |

Notes needs **no disk mount** — it is stateless w.r.t. local storage.

## Data model

- **Postgres `notes`** — the note bodies: `id`, `tenant`, `slug`, `title`, `content`,
  `created_at`, `updated_at`; unique on `(tenant, slug)`.
- **Postgres `note_versions`** — immutable per-save snapshots (ADR-0046): `id` (the opaque
  `version_id`), `tenant`, `slug`, `title`, `content`, `created_at`; indexed on
  `(tenant, slug)`. Deduped on the newest body and pruned to the latest 50 per note.
- **Qdrant `<tenant>__notes`** — note chunk embeddings (cosine), one collection per tenant.
  Each point payload: `{slug, chunk_index, heading, text}`.

Everything is tenant-scoped: the Postgres rows, the Qdrant collection name, and the NATS
subject.

## Dependencies

core-app (embeddings + status/page/attachment proxy via the platform API) · Qdrant
(vectors) · Postgres (note bodies) · NATS (the `notes.saved` event).

## Run & extend

```bash
docker compose up -d notes
```

Package `epicurus_notes`:

| Module | Responsibility |
| --- | --- |
| `chunker.py` | Heading-aware markdown splitter. |
| `db.py` | The `notes` + `note_versions` tables + CRUD (`NotesStore`) — the source of truth, incl. version snapshots. |
| `indexer.py` | Chunk + embed + upsert into `<tenant>__notes` (`NotesIndexer`); no search method (attach-only). |
| `pages.py` | The `editor` page surface: list, read, create/update (title derivation + slug safety + re-index). |
| `attachments.py` | The chat-attachment picker + resolve (`NotesAttachments`). |
| `service.py` | The manifest — `pages`, `attachable`, the `notes.saved` event, **no tools**. |
| `app.py` | Lifespan, `GET /status`, the `/pages/*` and `/attachments/*` routers, event publish. |
| `settings.py` | `NotesSettings` (adds Qdrant, DB, platform URL, chunk size). |
