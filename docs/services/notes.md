# notes — author Obsidian-style notes into a private collection

**`epicurus-notes`** is a sidecar module for writing notes in the ε editor. A note is
saved to Postgres (the source of truth) and indexed into its **own** tenant-scoped Qdrant
collection, so notes are RAG-ready — but they are **private**: the agent **never reads a
note's body**. It can see what notes exist and propose changes, but a note's content reaches
the assistant *only* when the user attaches it to a message. This is the line between
**Notes** (private; you author + manually attach, the agent proposes) and
[**Knowledge**](knowledge.md) (your vault, agent-retrievable). Host port **8092**.

Unlike knowledge — which indexes an Obsidian vault that lives on disk — notes are authored
in the app, so their content is **externalized state** (Postgres), not local disk
(constraint #2). Embeddings are obtained **through the core** (no model key lives here).

Each saved note is also **mirrored** as `<slug>.md` under `/data/notes` in the **shared file
space** (#KB-refactor), so notes appear in the storage module's Files view and can be read in
its split-screen reader alongside the knowledge base. Postgres stays the **source of truth**;
the mirror is read-only output, kept current on every save (best-effort, never failing a
save), with a one-time startup backfill of pre-existing notes.

## Privacy — what the agent can and cannot do

Notes are private. The boundary is enforced in two places (#KB-refactor):

- **No read tool.** The module exposes structure-only and write tools, but **no get/read
  tool** — the agent can never pull a note's body through MCP. It learns titles and slugs
  (to target the right note), and it proposes changes, but it does not see content.
- **Hidden from the file tools.** The `.md` mirror lives under `notes/` in the shared file
  space. The storage module hides that subtree from the **agent's** file tools, so the agent
  cannot read a note body via `storage_read`/`storage_list`/`storage_search` either (see
  [storage](storage.md)). The **operator-facing** Files page, `/read`, and `/download` are
  unaffected — you still browse and read your notes there.
- **Content reaches the agent only via attach** (unchanged): the user explicitly attaches a
  note to a turn, and the core injects its body into that turn's context.

Every agent write is **staged for operator review** (ADR-0033, the same flow as the knowledge
base) — nothing is written until you approve it.

## The contract it exposes

### MCP tools (agent-facing)

The agent has a **write-only** surface: it can list notes (titles/slugs only) and propose
changes, but it has **no read access** to a note's body. Every write is staged for your
review (ADR-0033) — nothing changes until you approve it.

**Read-only structure** (titles + slugs only — never bodies):

| Tool | Inputs | Returns |
| --- | --- | --- |
| `notes_list()` | — | Your notes as `title — slug`, newest first. **Never returns bodies.** |
| `notes_tree()` | — | The notes grouped/indented by slug path (folders inferred from `/` in slugs). Titles only, **never bodies**. |

**Proposals** — every change is **staged for operator review** (ADR-0033), never applied
directly; the operator approves or rejects it in the **Note suggestions** page:

| Tool | Inputs | Returns |
| --- | --- | --- |
| `notes_create(slug, content, note="")` | `slug`: the note id (e.g. `meeting-2026-06-24` or `work/ideas`); `content`: full body; `note`: optional rationale | A confirmation that a `create` was staged. The title is derived from the body. |
| `notes_propose_edit(slug, content, note="")` | `slug`; `content`: the **full** new body; `note` | A confirmation that an `update` (full-body replace) was staged. Since notes are private the agent cannot read the current body — it proposes the whole new content and you review the diff. |
| `notes_append(slug, text, note="")` | `slug`; `text`: the text to add; `note` | A confirmation that an `append` was staged. The agent supplies **only the text to add** (it cannot read the note); the server concatenates it onto the current body **on approval**. |
| `notes_delete(slug, note="")` | `slug`; `note` | A confirmation that a `delete` was staged; the note is removed only on approval. |

There is **deliberately no read/get tool** — a note's content reaches the agent only via
attach (see below). A slug is validated (non-empty, ≤ 512 chars, no control characters);
an invalid slug or operation comes back as an error, not a staged suggestion.

### Chat attachments (ADR-0019)

The module is an **attachment source** (`attachable: true`) — the only path from a note's
**content** into the agent's context:

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

### Note suggestions page (`review` archetype, ADR-0033)

The agent's note changes are **staged for review, never applied directly**. The module
contributes a second left-nav page — **Note suggestions** — declared as
`{id: "review", archetype: "review"}`, where the operator reviews and approves or rejects each
pending change. Only an approved change is written and indexed. This mirrors the knowledge
base's Suggestions page, so the core's cross-module feed (`GET /platform/v1/suggestions`) and
the shared review overlay render note suggestions with no special-casing.

A note suggestion carries one of four **operations**: `create` / `update` / `append` /
`delete`. The first three are content ops shown with a **server-computed unified diff** (and
support **per-hunk** approval); `delete` is reviewed as a confirmation showing the current
body. Because notes are slug-keyed in Postgres (no filesystem), there is **no** `move` /
folder operation; `append` is notes-specific — the agent supplies only the text to add and the
server concatenates it onto the current body. The review payload carries the full `current`
(live body, empty for a create) and `content` (the body approving would produce) so the shell
can render the per-hunk diff.

The **trust boundary is the author**: agent changes route through review; the operator's own
editor saves stay immediate, since the operator is already the approver. Approve/reject are
operator-only endpoints the core proxies — deliberately **not** MCP tools, so the agent cannot
approve its own proposals.

- `GET /pages/review` — the pending queue: each suggestion as `{id, title, path, operation,
  origin, note, created_at, diff, to_path, current, content}`. `path` is the note slug;
  `to_path` is always empty (present only for shape parity with knowledge); `operation` ∈
  `create`/`update`/`append`/`delete`.
- `POST /pages/review/suggestions/{id}/approve` — apply the change and drop it from the queue:
  create/update/append write the body + re-index; delete removes the note (row, vectors, and
  `.md` mirror). The body is **optional** `{content}` — the operator's per-hunk-merged result
  for a content op, so only the accepted changes are written; absent ⇒ compose from the
  current body (append concatenates, update/create use the proposal).
- `POST /pages/review/suggestions/{id}/reject` — discard the suggestion; nothing is touched.

Pending suggestions are stored in `notes_suggestions` (tenant-scoped — see *Data model*).
The review router is registered **before** the editor pages router so its literal
`/pages/review` route wins over the editor's `/pages/{page_id}` path parameter.

### Events (NATS)

Emits **`<tenant>.notes.saved`** (`{slug}`) after a note is saved and indexed.

### Web UI (manifest, ADR-0007 Tier 1)

| Panel | What it shows / does |
| --- | --- |
| **Status** | `note_count` · `last_updated_at`. Polled from `GET /status` via the core's `GET /platform/v1/modules/notes/status` proxy. |

No settings, no actions, no module code in the shell — Notes has no configurable surface; all
data flows through the core.

### HTTP

| Endpoint | Description |
| --- | --- |
| `GET /health` | Liveness probe. |
| `GET /metrics` | Prometheus metrics. |
| `GET /manifest` | Module manifest (tools, the `notes.saved` event, `attachable: true`, `pages`, UI). |
| `GET /status` | Live stats `{note_count, last_updated_at}`. Proxied at `GET /platform/v1/modules/notes/status`. |
| `GET /pages/{page_id}` | Editor document list `{title, docs:[{id, title, path}], can_create: true}` (page id `notes`). |
| `GET /pages/{page_id}/doc?path=<slug>` | One note's content `{path, title, content}`. |
| `PUT /pages/{page_id}/doc?path=<slug>` | Save (create-on-absent) `{content}` → `{path, indexed, chunk_count}`. The note is the source of truth — a failed re-index returns `indexed: false`, never losing the write. |
| `GET /pages/review` | Pending note-suggestion queue: `{title, suggestions:[{id, title, path, operation, origin, note, created_at, diff, to_path, current, content}]}`. `operation` ∈ create/update/append/delete. Proxied at `GET /platform/v1/modules/notes/pages/review`. Registered before the editor pages router so it isn't shadowed by `/pages/{page_id}`. |
| `POST /pages/review/suggestions/{sid}/approve` | Apply a staged change + index it, drop the row → `{id, status, path, operation, indexed}`. Optional `{content}` body — the operator's per-hunk-merged result for a content op; absent ⇒ compose from the current body. 404 if unknown. Operator-only (not an MCP tool). |
| `POST /pages/review/suggestions/{sid}/reject` | Discard a staged change, note untouched → `{id, status, path, operation}`. 404 if unknown. Operator-only. |
| `GET /attachments` | Attachment picker (see above). |
| `GET /attachments/{ref_id}` | Attachment resolve (see above). |
| `GET /mcp` (streamable-HTTP) | MCP surface: the structure (`notes_list`/`notes_tree`) and write (`notes_create`/`notes_propose_edit`/`notes_append`/`notes_delete`) tools — **no read tool**. |

## How saving + indexing works

1. The editor `PUT`s a note's full content to its slug (an in-app save), or an approved
   suggestion writes the composed body.
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
retrieval path queries it today** — and the agent has no read tool, so attach (which reads the
note body straight from Postgres) is the only way a note's content reaches the assistant. The
collection exists so a future, opt-in retrieval feature needs no re-index.

## Configuration

`NotesSettings` extends [`CoreSettings`](../reference/config.md):

| Env var | Default | Meaning |
| --- | --- | --- |
| `PLATFORM_URL` | `http://core-app:8080` | The core's base URL (embeddings via the platform API). |
| `QDRANT_URL` | `http://qdrant:6333` | Vector index. |
| `DATABASE_URL` | `postgresql+asyncpg://…/epicurus` | Note bodies + the suggestion queue (source of truth). |
| `NOTES_ROOT` | `/data/notes` | Notes' folder in the shared file space — each saved note is mirrored here as `<slug>.md` so it shows in the storage Files view (#KB-refactor). Postgres stays the source of truth; the mirror is read-only output. |
| `CHUNK_MAX_CHARS` | `2000` | Max chars per chunk before a hard split. |
| `NOTES_PORT` | `8092` | Host port (loopback-bound by default). |

Postgres remains the source of truth — notes are authored in-app, so their bodies are
externalized state, not local disk (constraint #2). The container does mount the **shared
file space** (`EPICURUS_FILES_ROOT`, the same `/data` volume storage and knowledge use)
**read-write** at `/data/notes` purely to write the `.md` mirror; the one-shot `files-init`
container creates and chowns that folder to uid 10001 first so a save never hits a
`PermissionError`. Losing the mirror never loses a note. The agent's view of `notes/` through
the storage file tools is hidden by storage's `STORAGE_AGENT_HIDDEN_PREFIXES` (default `notes`,
see [storage](storage.md)).

## Data model

- **Postgres `notes`** — the note bodies: `id`, `tenant`, `slug`, `title`, `content`,
  `created_at`, `updated_at`; unique on `(tenant, slug)`. **The source of truth.**
- **Postgres `notes_suggestions`** — pending agent-proposed note changes (ADR-0033):
  `id`, `tenant`, `sid` (opaque uuid), `slug`, `operation`
  (`create`/`update`/`append`/`delete`), `proposed_content` (the full body for
  create/update, the text to add for append, empty for delete), `origin`, `note`,
  `created_at`. A row is removed on approve (after the change is applied) or reject; the table
  only ever holds pending suggestions.
- **Qdrant `<tenant>__notes`** — note chunk embeddings (cosine), one collection per tenant.
  Each point payload: `{slug, chunk_index, heading, text}`.
- **`/data/notes/<slug>.md`** — the read-only `.md` mirror in the shared file space (derived
  output, not a store of record; #KB-refactor). Written best-effort on each save and on a
  one-time startup backfill; slug-confined so a slug carrying `..` can never escape the
  notes folder. The storage module reads it (Files view + split-screen reader), but hides it
  from the agent's file tools.

Everything is tenant-scoped: the Postgres rows, the suggestion queue, the Qdrant collection
name, and the NATS subject.

## Dependencies

core-app (embeddings + status/page/attachment/suggestion proxy via the platform API) · Qdrant
(vectors) · Postgres (note bodies + suggestion queue) · NATS (the `notes.saved` event) · the
shared file space (the `.md` mirror, read by storage).

## Run & extend

```bash
docker compose up -d notes
```

Package `epicurus_notes`:

| Module | Responsibility |
| --- | --- |
| `chunker.py` | Heading-aware markdown splitter. |
| `db.py` | The `notes` table + CRUD (`NotesStore`) — the source of truth. |
| `indexer.py` | Chunk + embed + upsert into `<tenant>__notes` (`NotesIndexer`); no search method (private + attach-only). |
| `mirror.py` | The read-only `.md` mirror to the shared file space (#KB-refactor): `NotesMirror` (slug-confined `write` per save, `delete` on note removal, and a one-time `backfill`), best-effort throughout. |
| `pages.py` | The `editor` page surface: list, read, create/update (title derivation + slug safety + mirror write + re-index) + `delete_doc` (used by an approved `delete`). |
| `suggestions.py` | The `review` page surface (ADR-0033): the `notes_suggestions` store, `NoteSuggestionReview` (diff + apply on approve / discard on reject, across create/update/append/delete; approve takes optional per-hunk `content`), and `create_note_review_router`. Approve/reject are operator-only — never MCP tools. |
| `attachments.py` | The chat-attachment picker + resolve (`NotesAttachments`) — the only path to a note's content. |
| `service.py` | The manifest — `pages` (editor + review), `attachable`, the `notes.saved` event, and the agent's write-only tools (structure: `notes_list`/`notes_tree`; writes: `notes_create`/`notes_propose_edit`/`notes_append`/`notes_delete` — **no read tool**). |
| `app.py` | Lifespan (incl. the suggestion-store init + mirror backfill), `GET /status`, the review router (registered first) + the `/pages/*` and `/attachments/*` routers, event publish. |
| `settings.py` | `NotesSettings` (adds Qdrant, DB, platform URL, chunk size, `notes_root`). |
