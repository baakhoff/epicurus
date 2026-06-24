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

Each saved note is also **mirrored** as `<slug>.md` under `/data/notes` in the **shared file
space** (#KB-refactor), so notes appear in the storage module's Files view and can be read in
its split-screen reader alongside the knowledge base. Postgres stays the **source of truth**;
the mirror is read-only output, kept current on every save (best-effort, never failing a
save), with a one-time startup backfill of pre-existing notes.

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
markdown source/preview editor, Save, and — because the page sets `can_create` — a **New
note** control); the module ships **no markup** and only supplies data over three endpoints
the core proxies. Saving to a new slug **creates** the note; saving an existing one updates
it. Each save re-indexes the note into `<tenant>__notes`.

The shared `EditorView` is **core-owned**; notes reuses it (knowledge is the other user).
The note **title** is derived from the body (its first heading / line), so the
`{content}`-only save contract needs no title field; the **slug** (the editor `path`) is a
Postgres key, not a filesystem path — there is no traversal surface, only slug validation.

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
| `PUT /pages/{page_id}/doc?path=<slug>` | Save (create-on-absent) `{content}` → `{path, indexed, chunk_count}`. The note is the source of truth — a failed re-index returns `indexed: false`, never losing the write. |
| `GET /attachments` | Attachment picker (see above). |
| `GET /attachments/{ref_id}` | Attachment resolve (see above). |
| `GET /mcp` (streamable-HTTP) | MCP surface (no tools registered). |

## How saving + indexing works

1. The editor `PUT`s a note's full content to its slug.
2. The module derives the **title** from the body and **upserts** the row in Postgres
   (the source of truth) — written first so an edit is never lost.
3. It **mirrors** the note to `<notes_root>/<slug>.md` in the shared file space so it shows
   in Files (#KB-refactor) — best-effort, never raising, and done before indexing so the
   file reflects the saved body even if the embed round-trip fails.
4. It then **chunks** the body heading-aware (hard-splitting past `CHUNK_MAX_CHARS`),
   **embeds** each chunk via the core's [`PlatformClient`](../reference/platform-client.md)
   (`POST /platform/v1/embed`, **no model key here**), and **upserts** the vectors into
   `<tenant>__notes` (stale vectors for the slug are dropped first).
5. On success it publishes `notes.saved`. If the embed round-trip fails (e.g. the core is
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
| `NOTES_ROOT` | `/data/notes` | Notes' folder in the shared file space — each saved note is mirrored here as `<slug>.md` so it shows in the storage Files view (#KB-refactor). Postgres stays the source of truth; the mirror is read-only output. |
| `CHUNK_MAX_CHARS` | `2000` | Max chars per chunk before a hard split. |
| `NOTES_PORT` | `8092` | Host port (loopback-bound by default). |

Postgres remains the source of truth — notes are authored in-app, so their bodies are
externalized state, not local disk (constraint #2). The container does mount the **shared
file space** (`EPICURUS_FILES_ROOT`, the same `/data` volume storage and knowledge use)
**read-write** at `/data/notes` purely to write the `.md` mirror; the one-shot `files-init`
container creates and chowns that folder to uid 10001 first so a save never hits a
`PermissionError`. Losing the mirror never loses a note.

## Data model

- **Postgres `notes`** — the note bodies: `id`, `tenant`, `slug`, `title`, `content`,
  `created_at`, `updated_at`; unique on `(tenant, slug)`. **The source of truth.**
- **Qdrant `<tenant>__notes`** — note chunk embeddings (cosine), one collection per tenant.
  Each point payload: `{slug, chunk_index, heading, text}`.
- **`/data/notes/<slug>.md`** — the read-only `.md` mirror in the shared file space (derived
  output, not a store of record; #KB-refactor). Written best-effort on each save and on a
  one-time startup backfill; slug-confined so a slug carrying `..` can never escape the
  notes folder. The storage module reads it (Files view + split-screen reader).

Everything is tenant-scoped: the Postgres rows, the Qdrant collection name, and the NATS
subject.

## Dependencies

core-app (embeddings + status/page/attachment proxy via the platform API) · Qdrant
(vectors) · Postgres (note bodies) · NATS (the `notes.saved` event) · the shared file space
(the `.md` mirror, read by storage).

## Run & extend

```bash
docker compose up -d notes
```

Package `epicurus_notes`:

| Module | Responsibility |
| --- | --- |
| `chunker.py` | Heading-aware markdown splitter. |
| `db.py` | The `notes` table + CRUD (`NotesStore`) — the source of truth. |
| `indexer.py` | Chunk + embed + upsert into `<tenant>__notes` (`NotesIndexer`); no search method (attach-only). |
| `mirror.py` | The read-only `.md` mirror to the shared file space (#KB-refactor): `NotesMirror` (slug-confined `write` per save + a one-time `backfill`), best-effort throughout. |
| `pages.py` | The `editor` page surface: list, read, create/update (title derivation + slug safety + mirror write + re-index). |
| `attachments.py` | The chat-attachment picker + resolve (`NotesAttachments`). |
| `service.py` | The manifest — `pages`, `attachable`, the `notes.saved` event, **no tools**. |
| `app.py` | Lifespan (incl. the mirror backfill), `GET /status`, the `/pages/*` and `/attachments/*` routers, event publish. |
| `settings.py` | `NotesSettings` (adds Qdrant, DB, platform URL, chunk size, `notes_root`). |
