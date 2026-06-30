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

Each saved note is also **mirrored** as `<slug>.md` under `notes/` in the **shared file space**
(tenant-scoped, constraint #1; #KB-refactor; #357/ADR-0065), so notes appear in the unified
Files view and can be read in its split-screen reader alongside the knowledge base. Notes
**no longer mounts the shared `/data` volume** — the mirror is written **through the core file
API** (`PlatformClient.files_*`, core path `notes/<rel>`), so the core performs the on-disk
write into its read-write `/data/<tenant>/notes`; `NOTES_ROOT` is now just the logical base for
the mirror's path mapping, not a mount. Postgres stays the **source of truth**; the mirror is
write-only output, kept current on every save (best-effort, never failing a save), with a
one-time startup backfill of pre-existing notes.

## Privacy — what the agent can and cannot do

Notes are private. The boundary is enforced in two places (#KB-refactor):

- **No read tool.** The module exposes structure-only and write tools, but **no get/read
  tool** — the agent can never pull a note's body through MCP. It learns titles and slugs
  (to target the right note), and it proposes changes, but it does not see content.
- **Hidden from the file tools.** The `.md` mirror lives under `notes/` in the shared file
  space. The storage module hides that subtree from the **agent's** file tools, so the agent
  cannot read a note body via `storage_read`/`storage_list`/`storage_search` either (see
  [storage](storage.md)). The **operator-facing**, core-owned Files surface (browse / read /
  download — ADR-0063) is unaffected — you still browse and read your notes there.
- **Content reaches the agent only via attach** (unchanged): the user explicitly attaches a
  note to a turn, and the core injects its body into that turn's context.

Every agent write is **staged for operator review** (ADR-0033, the same flow as the knowledge
base) — nothing is written until you approve it, unless you turn review **off** for notes, in
which case the agent's changes apply directly (see *Note suggestions page* below).

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
`{id: "notes", archetype: "editor"}`. The **core renders** the editor (a document/folder
tree, a markdown editor that **opens rendered** and **saves on leave / idle / explicit Save**
— not per keystroke, since each save re-embeds (ADR-0042) — and — because the page sets
`can_manage_files` — the file-management controls: **New document**, **New folder**,
new-file-in-folder, rename, delete); the module ships **no markup** and only supplies data
over the endpoints the core proxies. Saving to a new slug **creates** the note; saving an
existing one updates it. Each save re-indexes the note into `<tenant>__notes`.

The shared `EditorView` is **core-owned**; notes reuses it (knowledge is the other user).
The note **title** is derived from the body (its first heading / line), so the
`{content}`-only save contract needs no title field; the **slug** (the editor `path`) is a
Postgres key, not a filesystem path — there is no traversal surface, only slug validation.

**Folders, no projects (#KB-refactor).** A `/` in a slug groups notes into folders, exactly
like the knowledge base — but notes are a single **flat space** with **no project switcher**
(`scope_noun` is empty). The editor's folder controls let the operator organise notes into
nested folders; an **empty** folder is persisted in the `note_folders` table (see *Data
model*) so it survives a reload before any note is filed under it. Renaming a note (the
editor renames files, not folders) **re-keys its slug** via `POST /pages/{id}/move`: the row,
its vectors, and its `.md` mirror all follow the new slug. Folder management is the
**operator's** surface — the agent has no folder/move tool and notes stay private.

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
body. There is **no** `move` / folder **suggestion** operation — folders are the operator's,
managed in the editor (the agent has no folder/move tool); `append` is notes-specific — the
agent supplies only the text to add and the server concatenates it onto the current body. The
review payload carries the full `current` (live body, empty for a create) and `content` (the
body approving would produce) so the shell can render the per-hunk diff.

The **trust boundary is the author**: agent changes route through review; the operator's own
editor saves stay immediate, since the operator is already the approver. Approve/reject are
operator-only endpoints the core proxies — deliberately **not** MCP tools, so the agent cannot
approve its own proposals.

**Review on/off toggle (#KB-refactor).** The review page header carries a per-module switch —
*Review agent changes before applying* — backed by the core's
`GET/PUT /platform/v1/modules/notes/suggestions-enabled` (see [core-app](core-app.md)). When
**on** (the default), agent proposals stage here for approval as described above. When **off**,
the propose tools **apply the change directly** (the module reads the setting via its
`PlatformClient` and, if review is off, immediately approves its own staged suggestion through
the same apply path) and the tool reply says so. The operator's editor saves are immediate
regardless — the toggle only governs the **agent's** writes. If the setting can't be read the
module defaults to the safe path (review on).

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

**Version history (ADR-0046).** The page is **versioned** (`versioned: true`): every save
snapshots the note's body, and the shell offers a **browse + restore past versions**
affordance. A byte-identical re-save is deduped (no new snapshot), and history is bounded to
the newest **50** versions per note. **Restore is client-side** — the shell fetches a past
version and re-saves its content through the normal save path; the module exposes **no
restore endpoint**.

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
| `GET /manifest` | Module manifest (tools, the `notes.saved` event, `attachable: true`, `reindexable: true`, `pages`, UI). |
| `GET /status` | Live stats `{note_count, last_updated_at}`. Proxied at `GET /platform/v1/modules/notes/status`. |
| `POST /reindex` | **Force a full re-embed** of every note with the current embedding model → `{status: "started"}` (#332, ADR-0054). Drops the `<tenant>__notes` collection and re-embeds each note (notes are otherwise indexed only on save), so vectors built with a previous model are rebuilt. Runs in the background. Called by the core's re-embed fan-out (the manifest sets `reindexable`). |
| `GET /pages/{page_id}` | Editor document/folder tree `{title, docs:[{id, title, path, type}], can_manage_files: true}` (page id `notes`). Dir nodes (`type: "dir"`) come from `note_folders` ∪ slug prefixes, emitted parent-first before file nodes. |
| `GET /pages/{page_id}/doc?path=<slug>` | One note's content `{path, title, content}`. |
| `PUT /pages/{page_id}/doc?path=<slug>` | Save (create-on-absent) `{content}` → `{path, indexed, chunk_count}`. The note is the source of truth — a failed re-index returns `indexed: false`, never losing the write. Each save also snapshots the body for version history (ADR-0046). |
| `GET /pages/{page_id}/doc/versions?path=<slug>` | A note's past versions, newest first → `{versions:[{version_id, created_at, title, size}]}` (ADR-0046, capped at 50). |
| `GET /pages/{page_id}/doc/version?path=<slug>&version=<version_id>` | One past version's full content → `{path, version_id, created_at, title, content}`; 404 if it is not that note's version. |
| `POST /pages/{page_id}/folder?path=<dir>` | Create an empty folder → `{path}`. 409 if it exists, 400 if the path is invalid. |
| `DELETE /pages/{page_id}/doc?path=<slug>` | Delete a note (row + vectors + `.md` mirror). 404 if absent. Used by an approved `delete` and the editor's delete control. |
| `DELETE /pages/{page_id}/folder?path=<dir>` | Delete an **empty** folder. 409 if a note or child folder lives under it, 404 if it doesn't exist. |
| `POST /pages/{page_id}/move` | Rename/move a note `{from_path, to_path}` → `{path}` — re-keys the slug (row + vectors + mirror follow). 404 if the source is missing, 409 if the destination is taken. |
| `GET`/`PUT` `/platform/v1/modules/notes/suggestions-enabled` | (Core endpoint) the review on/off toggle for notes — see [core-app](core-app.md). |
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
3. It **mirrors** the note to `notes/<slug>.md` in the shared file space so it shows in Files
   (#KB-refactor; #357/ADR-0065) — written **through the core file API**
   (`PlatformClient.files_write`, core path `notes/<rel>`; the core does the on-disk write),
   best-effort, never raising, and done before indexing so the file reflects the saved body
   even if the embed round-trip fails. `NOTES_ROOT` only maps a slug to that core path; notes
   mounts no volume.
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
| `PLATFORM_URL` | `http://core-app:8080` | The core's base URL — embeddings **and the file API** (`PlatformClient.files_*`, for the `.md` mirror) via the platform API. |
| `QDRANT_URL` | `http://qdrant:6333` | Vector index. |
| `DATABASE_URL` | `postgresql+asyncpg://…/epicurus` | Note bodies + the suggestion queue (source of truth). |
| `NOTES_ROOT` | `/data/notes` | The **logical** base for the mirror's path mapping — **not a mount** (#357/ADR-0065). A note slug maps to the core file-API path `notes/<slug>.md` (the `/data` prefix is stripped); the **core** writes it into its read-write `<files-root>/<tenant>/notes` (`<tenant>` = `DEFAULT_TENANT_ID`). Each saved note is mirrored as `<slug>.md` so it shows in the unified Files view (#KB-refactor). Postgres stays the source of truth; the mirror is write-only output. |
| `CHUNK_MAX_CHARS` | `2000` | Max chars per chunk before a hard split. |
| `NOTES_PORT` | `8092` | Host port (loopback-bound by default). |

Postgres remains the source of truth — notes are authored in-app, so their bodies are
externalized state, not local disk (constraint #2). Since File-space Phase 4 (#357/ADR-0065)
the container **mounts no `/data` volume at all** — it reads nothing from disk (the indexer and
editor read Postgres), and its only file output, the `.md` mirror, is written **through the core
file API** (`PlatformClient.files_*`, core path `notes/<rel>`). The **core** owns the read-write
`/data/<tenant>` mount and performs the on-disk write (the on-disk tree stays tenant-scoped,
constraint #1; `<tenant>` = `DEFAULT_TENANT_ID`), so notes no longer needs `files-init` to chown
a notes folder for it — the core writes into a directory it owns. Losing the mirror never loses a
note. The agent's view of `notes/` through the storage file tools is hidden by storage's
`STORAGE_AGENT_HIDDEN_PREFIXES` (default `notes`, see [storage](storage.md)).

## Data model

- **Postgres `notes`** — the note bodies: `id`, `tenant`, `slug`, `title`, `content`,
  `created_at`, `updated_at`; unique on `(tenant, slug)`. **The source of truth.**
- **Postgres `note_folders`** — explicitly-created folders (#KB-refactor): `id`, `tenant`,
  `path`, `created_at`; unique on `(tenant, path)`. A folder is normally *implied* by a note
  slug containing `/`; this table makes an **empty** folder exist on its own so it survives a
  reload. Folders are derived from the union of these rows and every slug's prefixes.
- **Postgres `note_versions`** — immutable per-save snapshots (ADR-0046): `id` (the opaque
  `version_id`), `tenant`, `slug`, `title`, `content`, `created_at`; indexed on
  `(tenant, slug)`. Deduped on the newest body and pruned to the latest 50 per note.
- **Postgres `notes_suggestions`** — pending agent-proposed note changes (ADR-0033):
  `id`, `tenant`, `sid` (opaque uuid), `slug`, `operation`
  (`create`/`update`/`append`/`delete`), `proposed_content` (the full body for
  create/update, the text to add for append, empty for delete), `origin`, `note`,
  `created_at`. A row is removed on approve (after the change is applied) or reject; the table
  only ever holds pending suggestions.
- **Qdrant `<tenant>__notes`** — note chunk embeddings (cosine), one collection per tenant.
  Each point payload: `{slug, chunk_index, heading, text}`.
- **`notes/<slug>.md`** (core path) → **`/data/<tenant>/notes/<slug>.md`** on disk — the
  write-only `.md` mirror in the shared file space (tenant-scoped, constraint #1; derived output,
  not a store of record; #KB-refactor; #357/ADR-0065). Notes does **not** own this on disk —
  it writes it **through the core file API** (`PlatformClient.files_write`, core path
  `notes/<rel>`) and the **core** performs the on-disk write into the mount it owns. Written
  best-effort on each save and on a one-time startup backfill; the core's `normalize_rel`
  rejects any `..` so a slug can never escape the notes subtree. The unified Files surface reads
  it (Files view + split-screen reader); storage hides it from the agent's file tools.

Everything is tenant-scoped: the Postgres rows, the suggestion queue, the Qdrant collection
name, and the NATS subject.

## Dependencies

core-app (embeddings + **the file API for the `.md` mirror**, `PlatformClient.files_*` — plus
status/page/attachment/suggestion proxy, all via the platform API) · Qdrant (vectors) · Postgres
(note bodies + suggestion queue) · NATS (the `notes.saved` event). Notes **mounts no `/data`
volume** (#357/ADR-0065): its only file output goes through the core, which owns the file space;
the mirror is read back through the unified Files surface.

## Run & extend

```bash
docker compose up -d notes
```

Package `epicurus_notes`:

| Module | Responsibility |
| --- | --- |
| `chunker.py` | Heading-aware markdown splitter. |
| `db.py` | The `notes` + `note_versions` tables + CRUD (`NotesStore`) — the source of truth, incl. version snapshots (ADR-0046) — and the `note_folders` table + CRUD (`NoteFolderStore`) for explicit/empty folders (#KB-refactor). |
| `indexer.py` | Chunk + embed + upsert into `<tenant>__notes` (`NotesIndexer`); no search method (private + attach-only). |
| `mirror.py` | The write-only `.md` mirror to the shared file space via **the core file API** (#KB-refactor; #357/ADR-0065): `NotesMirror` maps a slug to core path `notes/<rel>` and calls `PlatformClient.files_write` / `files_delete` (`write` per save, `delete` on note removal, a one-time `backfill`), best-effort throughout. No direct disk I/O — the core performs the on-disk write. |
| `pages.py` | The `editor` page surface: tree list (folders + files), read, create/update (title derivation + slug safety + mirror write + version snapshot + re-index), `delete_doc`, the file-management ops (#KB-refactor): `create_folder` / `delete_folder` (empty-only) / `move_item` (slug re-key with vectors + mirror following), and `list_versions`/`get_version` (ADR-0046). |
| `suggestions.py` | The `review` page surface (ADR-0033): the `notes_suggestions` store, `NoteSuggestionReview` (diff + apply on approve / discard on reject, across create/update/append/delete; approve takes optional per-hunk `content`), and `create_note_review_router`. Approve/reject are operator-only — never MCP tools. |
| `attachments.py` | The chat-attachment picker + resolve (`NotesAttachments`) — the only path to a note's content. |
| `service.py` | The manifest — `pages` (editor + review), `attachable`, the `notes.saved` event, and the agent's write-only tools (structure: `notes_list`/`notes_tree`; writes: `notes_create`/`notes_propose_edit`/`notes_append`/`notes_delete` — **no read tool**). |
| `app.py` | Lifespan (incl. the suggestion-store + folder-store init + mirror backfill), `GET /status`, the review router (registered first) + the `/pages/*` and `/attachments/*` routers, event publish. |
| `settings.py` | `NotesSettings` (adds Qdrant, DB, platform URL, chunk size, `notes_root`). |
