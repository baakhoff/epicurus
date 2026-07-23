# knowledge — multi-project knowledge bases + platform self-documentation

**`epicurus-knowledge`** is a sidecar module that indexes three markdown sources
for retrieval-augmented generation, fully incrementally:

1. **Operator knowledge bases** — markdown notes the operator keeps under the shared
   file space at `/data/<tenant>/knowledge` (tenant-scoped, constraint #1). Each top-level
   folder there is a **project** ("knowledge base"); documents are addressed
   `<project>/<path>.md` (#KB-refactor).
2. **Platform docs** (self-documentation) — the `docs/` tree bundled into the image
   at `/docs`; available with **no operator setup** in any deploy. Also surfaced
   read-only inside the editor under a reserved `__docs__` scope so a service's
   documentation is browsable in the knowledge base (#KB-refactor).
3. **Module docs** — usage documentation contributed by each enabled module via a
   `docs_url` endpoint, auto-indexed on startup; disabled modules have their docs
   purged automatically (#215).

Chunks are embedded **through the core** (no model key lives here) and stored in
tenant-scoped Qdrant collections. Knowledge documents live under `/data/<tenant>/knowledge`
in the **shared file space** (tenant-scoped, constraint #1) — the same tree the core indexes
for the unified Files view (#KB-refactor). Since File-space Phase 3 (#356, ADR-0064) the
module's **writes** route through the core-owned file API (`PlatformClient.files_*`, core path
`knowledge/<rel>`); the read-path follow-up (#346, ADR-0070) moves the **reads** there too — the
incremental indexer, the editor's read/list, attachments, hover-cards, and the review diff all
go through the file API via a `VaultReader` seam, so in the default (normal) mode knowledge
**mounts no `/data` volume at all** and the read path follows the swappable local-FS ↔ S3
backend (constraint #3). The sole exception is **watch mode** (`VAULT_WATCH=true`, #232): the
inotify vault watcher has no file-API analogue, so a watched external Obsidian vault is read
from a disk mount, exactly as before. Host port **8085**.

## The contract it exposes

### MCP tools (agent-facing)

The agent **navigates** the knowledge base read-only and **proposes** every change — there
is no direct agent write path. The knowledge base is organised into **projects**
(top-level folders, each a "knowledge base"); documents are addressed `<project>/<path>.md`.

**Read-only navigation** (so the agent learns where things live):

| Tool | Inputs | Returns |
| --- | --- | --- |
| `knowledge_search(query, k=5)` | `query`: search phrase; `k`: max results | A `ToolEnvelope`: the top-`k` matching chunks as readable text **plus** one entity-reference chip per cited document (ADR-0019). |
| `knowledge_list_projects()` | — | The knowledge bases (projects) — their names, one per line (#KB-refactor). |
| `knowledge_tree(project="")` | `project`: optional knowledge-base name (omit for all) | An indented folder/document tree ("schema") of one or all knowledge bases. Paths are `<project>/<folder>/<doc>.md`. |
| `knowledge_read_document(path)` | `path`: `<project>/<folder>/<doc>.md` | One document's full content, or an error if the path is invalid or missing. |

**Reindex:**

| Tool | Inputs | Returns |
| --- | --- | --- |
| `knowledge_reindex()` | — | `{indexed, deleted, unchanged}` counts summed over all three sources (knowledge bases + platform docs + module docs). |

**Proposals** — every structural or content change is **staged for operator review**
(ADR-0033, #220), never applied directly; the operator approves or rejects it in the
Suggestions page:

| Tool | Inputs | Returns |
| --- | --- | --- |
| `knowledge_create_document(path, content, note="")` | `path`: knowledge-base-relative `.md` path of the **new** note (must not exist); `content`: full markdown; `note`: optional rationale | A confirmation that the new document was staged (or created, when review is off). The single-purpose **create** tool — reach for it to add a note instead of the multi-operation `knowledge_propose_edit` (#370). |
| `knowledge_propose_edit(path, content="", operation="update", note="")` | `path`: knowledge-base-relative `.md` path; `content`: proposed full content; `operation`: `create`/`update`/`delete` (default `update`); `note`: optional rationale | A confirmation that the change was staged. **Update/delete** of an existing note (to create one prefer `knowledge_create_document`); restricted to create/update/delete only — structural changes use the tools below (#KB-refactor). |
| `knowledge_propose_move(from_path, to_path, note="")` | the current and destination paths (file or folder); optional `note` | A confirmation that a move/rename was staged. |
| `knowledge_propose_rename(path, new_name, note="")` | `path`: the item's current `<project>/<…>/<name>`; `new_name`: the new **bare** leaf name (no `/`; the `.md` suffix is kept for documents); optional `note` | A confirmation that a rename-in-place was staged. A convenience over `knowledge_propose_move` — keeps the same folder. Staged as a `move` suggestion (#KB-refactor). |
| `knowledge_propose_folder(path, note="")` | `path`: `<project>/<folder>`; optional `note` | A confirmation that a folder create was staged. |
| `knowledge_propose_project(name, note="")` | `name`: a single folder name (no slashes); optional `note` | A confirmation that a new knowledge base create was staged. |

A rejected proposal (a path `safe_vault_rel` refuses, `knowledge_create_document` targeting an
existing path, or an operation `knowledge_propose_edit` doesn't handle) **fails the call** — a
raised error, MCP `isError` — rather than returning a success-shaped result: the live document
pane keys off that structural signal, not the reply text, so a rejected write must never look
like a staged suggestion (#690).

`knowledge_search` merges results from the operator's knowledge bases (`<tenant>__knowledge`)
and the platform-docs (`<tenant>__docs`) collections, re-ranked by cosine similarity score, so
the agent sees the most relevant content regardless of source. It returns a **`ToolEnvelope`**
(ADR-0019): the chunk text (so the agent can quote and reason over it) plus one
**entity-reference chip per distinct cited document** — hovering a chip shows a hover-card
and clicking a knowledge-base note opens it in the Knowledge page (see *Hover-cards* below).
Platform-docs citations are shown with a `docs/` path prefix so the agent can tell them
apart from knowledge-base notes.

### Events (the module event spine, #665)

Knowledge announces its world changes on the [module event spine](../reference/events.md) —
see the event catalog for payloads and dedup keys:

- **`events.knowledge.doc_created`** / **`events.knowledge.doc_deleted`** — immediate, at
  the editor save / file-tree action / approved suggestion that creates or removes a vault
  document. A whole-base removal (#340) emits no per-doc events (one operator action).
- **`events.knowledge.doc_updated`** — **debounced to settled saves** (the ADR-0042
  auto-save fires every ~4s idle pause): each save re-arms a per-doc quiet window
  (`KNOWLEDGE_EVENTS_DEBOUNCE_S`, default 120s) and one event fires when it passes
  untouched. A background sweeper flushes settled entries; pending ones flush on shutdown.
- **`events.knowledge.vault_synced`** — **one batch event per watcher pass** (#232) with
  the pass's counts (`indexed` merges added+updated); a no-op pass emits nothing, and the
  startup/initial index emits nothing (no-firehose).
- **`events.knowledge.index_failed`** — rate-limited
  (`KNOWLEDGE_INDEX_FAILED_COOLDOWN_S`, default 900s): the initial index giving up after
  its retry budget (#230), or a watcher pass failing.

All emission is best-effort — a spine hiccup never fails the change that already landed.
The legacy **`knowledge.index.completed`** declaration is gone: no code ever published it,
and the manifest now only advertises events that actually fire.

### Web UI (manifest, ADR-0007 Tier 1)

| Panel | What it shows / does |
| --- | --- |
| **Status** | `note_count` (vault notes) · `doc_count` (platform-docs pages) · `module_doc_count` (module-contributed docs) · `last_indexed_at` · `index_phase` / `index_attempts` (background-index progress, #230). Polled from `GET /status` via the core's `GET /platform/v1/modules/knowledge/status` proxy. |
| **Settings** | Vault path (`VAULT_PATH`, default `/data/knowledge`; the on-disk tree is tenant-scoped to `/data/<tenant>/knowledge`) — editable in the shell. |
| **Actions** | **Re-index** — triggers `knowledge_reindex` (all sources) through the core. |

No module code runs in the shell; all data flows through the core.

### Knowledge page (`editor` archetype, ADR-0018)

The module contributes a **Knowledge** left-nav page — an Obsidian-style browse-and-edit
view with nested folder management, declared as a `pages` entry
`{id: "vault", archetype: "editor"}`. The **core renders** the editor from its bounded
vocabulary (a knowledge-base switcher, a document/folder tree, a markdown editor that
**opens rendered** and **saves on leave / idle / explicit Save** — not per keystroke, since
each save re-embeds (ADR-0042) — a save button, CRUD controls); the module ships **no
markup** and only supplies data over the endpoints the core proxies.

**Projects / scopes (#KB-refactor).** The page is scoped to one **knowledge base** (project)
at a time. `EditorData` carries the list of selectable scopes and which is active:

- `scopes` — each `{id, title, kind}` where `kind` is `project` (a writable knowledge base)
  or `reference` (the read-only platform docs).
- `scope` — the active scope id; defaults to the first project.
- `scope_noun` — `"knowledge base"` (the noun the shell shows on the switcher and its
  "New …" control). An empty `scope_noun` means *no switcher* — Notes leaves it empty,
  keeping the shared archetype generic.
- `can_create_scope` — whether the operator may create another knowledge base.

The switcher lets the operator move between knowledge bases, **New knowledge base** creates a
top-level folder (`POST /pages/{page_id}/project?name=`), a per-project **Remove** (trash)
affordance deletes one behind a confirm dialog (`DELETE …/project`, dir + Qdrant points, #340),
and the shell offers a **New document** control (a root-level create — previously a document
could only be added inside an existing folder). Tree `docs` paths are **scope-relative** — the shell prepends the active
`scope` when it reads, saves, or manages files — so a project's contents show without the
project folder itself appearing as a node.

**Platform docs in the switcher (`__docs__` scope, #KB-refactor).** A reserved, read-only
scope surfaces the bundled platform docs inside the knowledge base, so a service's
documentation is browsable alongside the operator's notes. It is listed in `scopes` with
`kind: "reference"`, returns `read_only: true` / `can_manage_files: false`, and every write
that targets it is refused (**409**). The `__docs__` id is `_`-prefixed, which a real
project name can never be (`safe_project`), so the path scheme stays unambiguous.

Saving a document — on leaving the page, after it idles, or on an explicit Save (ADR-0042) —
writes it back to the knowledge base and **re-indexes just that file** into
`<tenant>__knowledge`, so an edit made in the shell is immediately retrievable by the
agent (knowledge is agent-retrievable by default — contrast the Notes module). The
editor component is **core-owned and shared**; Notes reuses it.

The operator experience is unchanged; only the **file-access mechanism** moved. Since
File-space Phase 3 (#356, ADR-0064) the save and all file-tree CRUD (create/delete folder,
delete doc, move/rename, create/delete knowledge base) write through the core file API
(`PlatformClient.files_*`) — a vault-relative path maps to the core path `knowledge/<rel>`, and
the core writes it to its read-write `/data/<tenant>` mount. The read-path follow-up (#346,
ADR-0070) then routes the editor's **read/list** (`read_doc` / `list_docs`) through the same
file API, so in normal mode the module holds **no `/data` mount** at all. Qdrant re-indexing
runs **after** the core write, unchanged.

**Read-only when the vault is externally owned (#232, ADR-0035).** With a watched external
vault (`VAULT_WATCH=true`, see *Live vault sync* below) the page returns `read_only: true`
and `can_manage_files: false`: the shell hides Save and the file-tree controls, **never
auto-saves**, and shows a read-only banner, and every write endpoint (save, folder create,
doc/folder delete, move, new knowledge base) returns **409**. Obsidian is the sole author;
edits made there sync to disk and re-index automatically.

**File-tree management (#216).** The Knowledge page sets `can_manage_files: true` in its
`EditorData` response; the shell then shows CRUD controls over the tree — creating nested
folders, creating documents inside any folder, deleting files or empty folders, and
renaming documents. Operations are gated by path-safety validation in `refs.py` (same
`..`-traversal and symlink-escape checks as the document editor). Notes sets this flag
`false` — it uses the separate `can_create` authoring flow instead.

**Version history (#ADR-0046).** The Knowledge page is `versioned: true` in its `EditorData`
response: **every editor save snapshots the document's content** into the
`knowledge_versions` table (see *Data model*), so the shell can browse and **restore** a
prior revision. Restore is **client-side** — the shell fetches a past version's content via
the version endpoint and re-saves it through the normal `PUT .../doc` path (which snapshots
again); the module exposes **no** restore endpoint. Consecutive byte-identical saves are
**deduplicated** (an idle/blur auto-save that changed nothing adds no row), and the newest
**50** versions per `(tenant, path)` are retained — older snapshots are pruned. Recording a
snapshot is best-effort and never fails a save: the file write is the source of truth, so a
version is recorded even if the re-index failed. Viewing history (`GET .../doc/versions`,
`GET .../doc/version`) is allowed even on a **read-only (watched) vault** — but because every
write 409s there (#232), **external/watched-vault edits are not versioned in v1**: only
in-app editor saves accrue history. Browse the list newest-first; each entry carries an
opaque `version_id`, the snapshot `title`, its `created_at`, and its `size`.

### Suggestions page (`review` archetype, ADR-0033, #220; edit-before-approve + audit, ADR-0090)

Agent-initiated knowledge-base changes are **staged for review, never applied directly**.
Every agent write — content (`knowledge_propose_edit`) *and* structural
(`knowledge_propose_move` / `knowledge_propose_folder` / `knowledge_propose_project`) —
stages a suggestion; the module contributes a second left-nav page — **Suggestions** —
declared as `{id: "review", archetype: "review"}`, where the operator reviews and approves
or rejects each pending change. Only an approved change is written and indexed.

A suggestion carries one of six **operations**: `create` / `update` / `delete` (content
ops, with a server-computed unified diff) and `move` / `mkdir` / `mkproject` (structural
ops, reviewed as a simple confirmation from `path` / `to_path`). The review payload includes
the full `current` (live document, empty for a create) and `content` (the proposal, empty for
a delete) so the shell can render a **per-hunk** review of an edit, plus an **editable draft**
the operator can hand-edit directly before approving (ADR-0090 — "edit anywhere before
approving anything"; #KB-refactor shipped the per-hunk merge this builds on).

The **trust boundary is the author**: agent edits route through review; direct *operator*
edits (the editor save, the file-tree CRUD) stay immediate, since the operator is already the
approver. Approve/reject are operator-only endpoints the core proxies — deliberately **not**
MCP tools, so the agent cannot approve its own proposals. Approving (or, with review off,
the propose tool's self-apply) performs its write through the same core file API as the
editor save (`PlatformClient.files_*`, core path `knowledge/<rel>`) — #356, ADR-0064 — then
re-indexes; only the write mechanism changed, the review flow is unchanged.

**Review on/off toggle (#KB-refactor).** The Suggestions page header carries a per-module
switch — *Review agent changes before applying* — backed by the core's
`GET/PUT /platform/v1/modules/knowledge/suggestions-enabled` (see [core-app](core-app.md)).
When **on** (the default) proposals stage here for approval. When **off**, the propose tools
**apply the change directly**: the module reads the setting via its `PlatformClient` and, if
review is off, immediately approves its own staged suggestion through the same apply path
(so a content op still re-indexes, a structural op still relocates). If the setting can't be
read the module defaults to the safe path (review on). A watched read-only vault still 409s
on apply regardless of the toggle — when that self-apply 409s (or fails any other way), the
suggestion stays staged (nothing is lost) but the *tool call* fails structurally (a raised
error, MCP `isError`) rather than reporting success: the caller asked for an immediate write
and it did not happen, so the live document pane must not treat it as applied (#690).

With a **watched external vault** (`VAULT_WATCH=true`, #232) the vault is read-only to
epicurus, so **approve returns 409** — applying would write a vault Obsidian owns. The agent
can still propose and the operator can still *reject* to clear the queue; the operator makes
the change in Obsidian instead (ADR-0035).

- `GET /pages/review` — the pending queue: each suggestion as `{id, title, path, operation,
  origin, note, created_at, diff, to_path, current, content}`, where `diff` is a
  server-computed unified diff of the current content against the proposal (empty for a
  structural op).
- `POST /pages/review/suggestions/{id}/approve` — apply the change and drop it from the
  queue: create/update write + re-index; delete unlinks + de-indexes; move relocates +
  re-indexes; mkdir/mkproject create a folder / knowledge base. The body is **optional**
  `{content}` — the operator's **edited draft** (ADR-0090: a free-form edit, a per-hunk
  merge, or both), so what's written is what was actually approved; absent ⇒ apply the
  agent's full proposal unedited.
- `POST /pages/review/suggestions/{id}/reject` — discard the suggestion; nothing is touched.
- `GET /pages/review/audit?limit=` — the resolved-decision **audit trail** (ADR-0090), newest
  first: each entry pairs the agent's original `proposed_content` with the operator's
  `applied_content` (empty for a reject, or a content-less structural op), plus `decision`
  (`approved`/`rejected`) and `decided_at`. `limit` defaults to 50.

Pending suggestions are stored in `knowledge_suggestions` (tenant-scoped — see *Data model*);
approve/reject record a row in `knowledge_suggestion_decisions` before dropping the pending
row, since the queue itself holds only pending suggestions (ADR-0033) — the decisions table
is the durable trail. Capped at `MAX_DECISIONS` (200) rows per tenant, pruned oldest-first.

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
| `GET /manifest` | Module manifest (tools, events, UI declaration, **`pages`**, **`attachable`**, **`resolver`**, **`reindexable`**). |
| `GET /status` | Live index stats: `{note_count, doc_count, module_doc_count, last_indexed_at, index_phase, index_attempts}`. `index_phase` ∈ `pending`/`indexing`/`ready`/`retrying`/`error` (#230). Proxied by the core at `GET /platform/v1/modules/knowledge/status`. |
| `POST /reindex` | **Force a full re-embed** of every source (vault + platform docs + module docs) with the current embedding model → `{status: "started"}` (#332, ADR-0054). Unlike the incremental `knowledge_reindex` tool, this **drops the Qdrant collections and clears the ledgers first**, so vectors built with a previous model are rebuilt rather than skipped as "unchanged". Runs in the background; watch `GET /status`. Called by the core's re-embed fan-out (the manifest sets `reindexable`). |
| `GET /pages/{page_id}?scope=<id>` | Editor document/folder tree `{title, docs:[{id, title, path, type}], can_manage_files, read_only, versioned, scopes:[{id, title, kind}], scope, scope_noun, can_create_scope}` (page id `vault`). `scope` selects the knowledge base (empty = the first project, or the reserved `__docs__` for the read-only platform docs). `type` is `"file"` or `"dir"`; `docs` paths are scope-relative. `can_manage_files: true` enables folder CRUD; `versioned: true` enables save-history browse/restore (#ADR-0046); `read_only: true` (watch mode #232, or the `__docs__` scope) makes the page view-only. Proxied at `GET /platform/v1/modules/knowledge/pages/{page_id}`. |
| `POST /pages/{page_id}/project?name=<name>` | Create a new knowledge base — a top-level folder under the knowledge root → `{id, title, kind}` (#KB-refactor). 409 if it already exists, 400 for an invalid name (single segment, no separators / `..` / `.`/`_` prefix). Proxied at `POST /platform/v1/modules/knowledge/pages/{page_id}/project`. |
| `DELETE /pages/{page_id}/project?name=<name>` | Delete a knowledge base — removes the top-level folder **and de-indexes its documents** (drops every Qdrant vector + ledger row under `<name>/`, tenant-scoped) so it leaves search at once (#340). `204` on success. 404 if absent, 400 for an invalid name, **409** when the vault is read-only (watch mode, #232). The operator's **Remove** affordance — the agent never deletes a base (no tool, no review op). Proxied at `DELETE /platform/v1/modules/knowledge/pages/{page_id}/project`. |
| `GET /pages/{page_id}/doc?path=<rel>` | One document's content `{path, title, content}`. `path` is scope-relative and strictly confined (no traversal, `.md` only); a `__docs__/…` path reads the read-only platform docs. |
| `PUT /pages/{page_id}/doc?path=<rel>` | Save a document `{content}` → `{path, indexed, chunk_count}`; writes the file then re-indexes it, and records a version-history snapshot (#ADR-0046, see *Version history* below). The write is the source of truth — a failed re-index returns `indexed: false`, never losing the edit. **409** when the vault is externally owned (watch mode, #232) or the path targets the read-only `__docs__` scope; the folder/delete/move write routes behave likewise. |
| `GET /pages/{page_id}/doc/versions?path=<rel>` | A document's save-snapshot history (#ADR-0046), newest first → `{versions:[{version_id, created_at, title, size}]}`. `version_id` is opaque; `size` is the snapshot's character count. Allowed even when the vault is read-only (viewing history is not a write). Proxied at `GET /platform/v1/modules/knowledge/pages/{page_id}/doc/versions`. |
| `GET /pages/{page_id}/doc/version?path=<rel>&version=<version_id>` | One past version's full content → `{path, version_id, created_at, title, content}`. 404 if the version is unknown (a non-integer `version_id` is treated as not-found, never a 500). Allowed when read-only. Proxied at `GET /platform/v1/modules/knowledge/pages/{page_id}/doc/version`. |
| `POST /pages/{page_id}/folder?path=<rel>` | Create a directory at `path` → `{path}`. 409 if the directory already exists. Path goes through `safe_dir_relative` (no `..`, no absolute). Proxied at `POST /platform/v1/modules/knowledge/pages/{page_id}/folder`. |
| `DELETE /pages/{page_id}/doc?path=<rel>` | Delete a `.md` file. 404 if absent. 400 for path-safety violations. Proxied at `DELETE /platform/v1/modules/knowledge/pages/{page_id}/doc`. |
| `DELETE /pages/{page_id}/folder?path=<rel>` | Delete an **empty** directory. 409 if not empty, 404 if absent. Proxied at `DELETE /platform/v1/modules/knowledge/pages/{page_id}/folder`. |
| `POST /pages/{page_id}/move` | Move or rename a file or folder, then re-index it (#470). Body: `{from_path, to_path}` → `{path, indexed}`. 404 if source absent, 409 if destination exists. The move is the source of truth — a failed re-index still returns 200 with `indexed: false`, mirroring the save endpoint, rather than losing the already-applied move. Proxied at `POST /platform/v1/modules/knowledge/pages/{page_id}/move`. |
| `GET /pages/review` | Pending suggestion queue (#220): `{title, suggestions:[{id, title, path, operation, origin, note, created_at, diff, to_path, current, content}]}`. `operation` ∈ create/update/delete/move/mkdir/mkproject; `diff`/`current`/`content` are empty for structural ops (#KB-refactor). Proxied at `GET /platform/v1/modules/knowledge/pages/review`. Registered before the editor pages router so it isn't shadowed by `/pages/{page_id}`. |
| `POST /pages/review/suggestions/{sid}/approve` | Apply a staged change + index it, drop the row → `{id, status, path, operation, indexed}`. Optional `{content}` body — the operator's per-hunk-merged result for an edit; absent ⇒ apply the full proposal (#KB-refactor). 404 if unknown; **409** when the vault is externally owned (watch mode, #232). Proxied at `POST /platform/v1/modules/knowledge/pages/review/suggestions/{sid}/approve`. Operator-only (not an MCP tool). |
| `POST /pages/review/suggestions/{sid}/reject` | Discard a staged change, vault untouched → `{id, status, path, operation}`. 404 if unknown. Proxied likewise. Operator-only. |
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

**Self-heal after a Qdrant reset (#229).** Qdrant vectors are derived data and may be
wiped when the server is upgraded across an incompatible on-disk format (the `qdrant-init`
guard — see [Qdrant](../infrastructure/qdrant.md)). When that happens the Postgres ledgers
still list every file as indexed, so a plain incremental run would skip everything and
leave the fresh collection empty. Each indexer therefore **reconciles** before indexing: if
its collection is missing but its ledger is non-empty, it clears the ledger so the run
re-embeds from scratch. The runner reconciles **all** sources up front — before any of them
recreates the shared `<tenant>__docs` collection — so the vault, platform docs, and module
docs all rebuild after a reset.

**Stale-path GC on every reconcile pass (#470).** When the collection is intact, `reconcile`
instead does a lighter check: any ledger row whose path the live vault no longer has is
dropped (its vectors purged too), with no content read or re-embed. This is a cheap,
periodic backstop for anything that orphans a path outside the normal move/delete flow —
running on every `IndexRunner` pass (startup, and every retry), so a stray stale entry is
never more than one pass away from clearing on its own.

### Live vault sync — the watched vault (#232, ADR-0035)

By default the index refreshes at startup and on an explicit `knowledge_reindex`. Set
**`VAULT_WATCH=true`** to also **watch the vault and re-index on change** — the path for
keeping the knowledge base in step with an Obsidian-Sync (or Git) folder bind-mounted under
`/data/<tenant>/knowledge` (the tenant-scoped `VAULT_PATH`). `watcher.VaultWatcher` runs a `watchfiles.awatch` loop; each debounced batch of
changes (window `VAULT_WATCH_DEBOUNCE_MS`, default 1500 ms) triggers one incremental
`KnowledgeIndexer.run()`. Because the indexer is hash/mtime-incremental, a watch event over
a synced folder only re-embeds the files that actually changed.

> **Watch mode needs the vault mounted (#346, ADR-0070).** In normal mode knowledge holds no
> `/data` mount — it reads through the core file API. Watch mode is the exception: its inotify
> watcher and its reads both need the vault on disk, so enabling `VAULT_WATCH=true` also
> requires a **compose override** that mounts your vault into the container. See
> [Keeping the vault in sync with Obsidian](../developer/obsidian-sync.md) for the exact override.

- **Debounced & scoped.** A burst (Obsidian Sync writes many files at once) coalesces into
  a single pass; `.obsidian/` and `.trash/` are ignored and only `.md` files trigger work.
  A deletion still carries its `.md` path, so removals flow through and their vectors are
  purged on the next pass.
- **Serialised.** `KnowledgeIndexer.run()` holds a run-lock, so a watch pass and the
  background startup index (#230) never walk the vault at once.
- **Resilient.** A failed pass (core paused mid-embed, a Qdrant blip) is logged and retried
  on the next change; the watcher never dies on a transient error. A missing vault leaves
  it idle rather than crashing the service.
- **Externally owned ⇒ read-only.** Watch mode marks the vault externally owned: the editor
  page is read-only, the file-tree CRUD is hidden, and applying an agent suggestion is
  refused (409). Obsidian is the sole author — this avoids two writers racing the same files
  (ADR-0035). See [Keeping the vault in sync with Obsidian](../developer/obsidian-sync.md)
  for the same-host (bind-mount) and headless (Git) setup recipes.

Off by default — the common image-only / empty-volume deploy starts no watcher.

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
| `VAULT_PATH` | `/data/knowledge` | Knowledge's path within the shared file space; the tree is tenant-scoped to `<files-root>/<tenant>/knowledge` (`<tenant>` = `DEFAULT_TENANT_ID`). Each top-level folder under it is a project (knowledge base). It lives under the `/data` tree the **core** mounts and indexes for the unified Files view (#KB-refactor, ADR-0063). Knowledge itself **mounts no `/data` volume** in normal mode: both reads and writes go through the core file API (core key `knowledge/<rel>`) — #346/ADR-0070. `VAULT_PATH` is still the in-container key prefix (its `.name`, `knowledge`, is the core prefix). Watch mode (`VAULT_WATCH=true`) is the exception and re-mounts this path from disk via an override (see [Obsidian sync](../developer/obsidian-sync.md)). |
| `DOCS_PATH` | `/docs` | In-container path of the platform docs (bundled in image). |
| `PLATFORM_URL` | `http://core-app:8080` | The core's base URL — for embeddings *and* the file API (`PlatformClient.files_*`), through which every knowledge write now flows (#356, ADR-0064). |
| `QDRANT_URL` | `http://qdrant:6333` | Vector index. |
| `DATABASE_URL` | `postgresql+asyncpg://…/epicurus` | File hash/mtime tracking. |
| `CHUNK_MAX_CHARS` | `2000` | Max chars per chunk before a hard split. |
| `EMBED_BATCH_SIZE` | `64` | Chunk texts embedded per `/embed` round-trip — the indexer flushes a batch once this many chunks are queued (#230). |
| `INDEX_RETRY_MAX_ATTEMPTS` | `30` | Background-index retry cap before giving up (#230). |
| `INDEX_RETRY_BASE_DELAY_SECONDS` | `1.0` | First retry backoff; doubles each attempt. |
| `INDEX_RETRY_MAX_DELAY_SECONDS` | `30.0` | Upper bound on the retry backoff. |
| `VAULT_WATCH` | `false` | Watch the vault and re-index on change (#232). Enabling it makes the vault **externally owned** — the editor page goes read-only and Obsidian becomes the sole author (ADR-0035). See [Obsidian sync](../developer/obsidian-sync.md). |
| `VAULT_WATCH_DEBOUNCE_MS` | `1500` | Coalescing window (ms) for a burst of vault changes before a re-index is triggered. |
| `KNOWLEDGE_EVENTS_DEBOUNCE_S` | `120` | Quiet window (s) a document must sit unsaved before `knowledge.doc_updated` fires on the event spine (#665) — one event per editing session, not per auto-save. |
| `KNOWLEDGE_INDEX_FAILED_COOLDOWN_S` | `900` | Minimum gap (s) between `knowledge.index_failed` emissions — a stuck vault must not storm the spine once per watcher wake (#665). |

Knowledge documents live at `/data/<tenant>/knowledge` in the **shared file space** — bound
into the **core** via `EPICURUS_FILES_ROOT` (the env var that mounts the `/data` tree; it
defaults to an **empty named volume**). Since File-space Phase 3 (#356, ADR-0064) every
**write** went through the core file API; the read-path follow-up (#346, ADR-0070) routes the
**reads** — the incremental indexer, the editor's read/list, attachments, the hover-card
resolver, the review diff, and the agent read tools — through the file API too, via a
`VaultReader` seam. So in the default (normal) mode **knowledge mounts no `/data` volume**: the
core is the sole mounter of `/data`, and knowledge reaches the space entirely over the platform
API (a vault-relative path resolves to the core key `knowledge/<rel>`). This satisfies
constraint #3 for knowledge — the read/write path follows the core's swappable local-FS ↔ S3
backend. The tree is **tenant-scoped** (constraint #1): the `<tenant>/` segment (`<tenant>` =
`DEFAULT_TENANT_ID`, default `local`) sits under `/data`. The **core image's entrypoint**
creates and chowns `/data/<tenant>` to uid 10001 before dropping privileges, so the core's
write of an editor create/save never hits a `PermissionError` on a fresh volume — and
knowledge's indexer tolerates a `knowledge/` subdir the core has not written yet (the file API
returns an empty listing, not an error) (#421/ADR-0069 — see
[Infrastructure](../infrastructure/index.md#shared-file-space)). The platform docs at `/docs`
are always present — bundled at image build time, read directly from that image mount (a disk
reader), **not** tenant-scoped (shared & read-only), and not editable from the shell.

**Watch mode is the exception (#232 + #346).** With a watched external vault
(`VAULT_WATCH=true`) the inotify watcher and the reads both need the vault on disk (there is no
inotify over the file API), so watch mode reads it from a **disk mount** exactly as before — and
because the default no longer mounts `/data`, enabling watch mode also requires a **compose
override** that mounts your Obsidian-synced folder under `/data/<tenant>/knowledge` (the
container user needs read access). `EPICURUS_FILES_ROOT` **replaces** the old per-module
`KNOWLEDGE_HOST_VAULT`. See
[Keeping the vault in sync with Obsidian](../developer/obsidian-sync.md) for the exact recipe.

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
- **Postgres `knowledge_suggestions`** — pending agent-proposed changes (#220, ADR-0033):
  `id`, `tenant`, `sid` (opaque uuid), `path`, `operation`
  (`create`/`update`/`delete`/`move`/`mkdir`/`mkproject`), `proposed_content`, `to_path`
  (the destination of a `move`, empty otherwise), `origin`, `note`, `created_at`. A row is
  removed on approve (after the change is applied) or reject; the table only ever holds
  pending suggestions. The `to_path` column is added in place at init on a pre-#KB-refactor
  deployment (the store uses `create_all`, no migration tool — mirrors `storage_files`).
- **Postgres `knowledge_suggestion_decisions`** — resolved-decision audit trail (ADR-0090):
  `id`, `tenant`, `sid`, `path`, `operation`, `origin`, `note`, `proposed_content`,
  `applied_content` (empty for a reject or a content-less structural op), `to_path`,
  `decision` (`approved`/`rejected`), `proposed_at`, `decided_at`. Append-only — recorded
  before the matching `knowledge_suggestions` row is dropped — and capped at the newest
  `MAX_DECISIONS` (200) rows per tenant, pruned in the same transaction as each insert.
- **Postgres `knowledge_versions`** — editor-save content snapshots (#ADR-0046): `id` (PK,
  also the opaque `version_id`), `tenant`, `note_path`, `title`, `content` (Text — full
  snapshot), `created_at`; indexed on `(tenant, note_path)`. One row per distinct save
  (consecutive identical saves deduplicated); pruned to the newest 50 per `(tenant,
  note_path)`. Shares the index ledgers' engine; created by `VersionStore.init()`.
- **Qdrant `<tenant>__knowledge`** — vault chunk embeddings (cosine), one collection per tenant.
- **Qdrant `<tenant>__docs`** — platform-docs + module-docs chunk embeddings (cosine), one
  collection per tenant. Module-doc points use a distinct UUID namespace from platform-doc
  points to avoid ID collisions in the shared collection.

Each Qdrant point payload: `{note_path, chunk_index, heading, text}`.

The **document bytes** themselves are not owned by this module's data model. They live in the
shared file space under the core's [`FileStore`](../reference/files.md) (`/data/<tenant>/knowledge`,
core path `knowledge/<rel>`): knowledge **reads and writes** them through the core file API
(`PlatformClient.files_*` — #356/ADR-0064 for writes, #346/ADR-0070 for reads), holding no
`/data` mount in normal mode. The Postgres ledgers and Qdrant collections above are derived
indexes over those files.

## Dependencies

core-app (embeddings, the **file API** for all reads *and* writes — `PlatformClient.files_*`,
#356/ADR-0064 for writes + #346/ADR-0070 for reads — and the status proxy, all via the platform
API) · Qdrant (vectors) · Postgres (file tracking) · NATS (the spine events, #665) · bundled
docs (an image mount). In **watch mode** only (#232), also a disk mount of the external vault
for the inotify watcher + its reads.

## Run & extend

```bash
# With a host directory for the shared file space (knowledge bases live under
# <root>/<tenant>/knowledge/<project>/, tenant = DEFAULT_TENANT_ID; docs auto-indexed from the image):
EPICURUS_FILES_ROOT=/path/to/your/files docker compose up -d knowledge

# Without one (empty named volume; only platform docs are indexed until you add notes):
docker compose up -d knowledge
```

Package `epicurus_knowledge`:

| Module | Responsibility |
| --- | --- |
| `chunker.py` | Heading-aware markdown splitter. |
| `db.py` | `knowledge_notes` ledger (`NoteIndex`) + `knowledge_doc_index` ledger (`DocIndex`); per-path `indexed_at` powers the hover-card's *Last indexed*. Also `knowledge_versions` (`VersionStore`): editor-save content snapshots with dedup + 50-version retention (#ADR-0046). |
| `indexer.py` | Diff + batched embed + upsert + semantic search (`KnowledgeIndexer`, parameterised by source **and a `VaultReader`**); the walk + single-file read go through the reader (the file API by default — #346/ADR-0070), deriving `mtime_ns` from the entry mtime; accumulates chunks across files and flushes per `EMBED_BATCH_SIZE` (#230); `index_path` re-indexes a single file for the editor save; `move_path` re-keys the index after a move — a single file swaps its vectors directly, a folder move reconciles via a full run (#470); a run-lock serialises full passes so the watcher (#232) and startup index never overlap; `reconcile` self-heals a wiped Qdrant collection (#229) and, when the collection is intact, GCs ledger rows for paths the vault no longer has (#470). |
| `runner.py` | `IndexRunner` (#230): runs every source indexer in the background with retry/backoff and exposes `IndexState` for `GET /status`; reconciles all sources up front to self-heal after a Qdrant reset (#229). |
| `watcher.py` | The vault file-watcher (#232): `VaultWatcher` (`watchfiles.awatch` → debounced incremental re-index) + `VaultChangeFilter` (ignore `.obsidian/`/`.trash/`, `.md` only). The one path that still reads the disk directly — inotify has no file-API analogue — so it (and the reads it triggers) run only in **watch mode**, where the vault is a disk mount (#346/ADR-0070). Started by `app.py` when `VAULT_WATCH=true`. |
| `service.py` | MCP tools — read-only navigation (`knowledge_search` → entity-ref chips, `knowledge_list_projects`, `knowledge_tree`, `knowledge_read_document`), `knowledge_reindex`, and the write tools that stage suggestions (`knowledge_create_document` (create), `knowledge_propose_edit` update/delete, `knowledge_propose_move`, `knowledge_propose_rename` (rename-in-place → a `move` suggestion), `knowledge_propose_folder`, `knowledge_propose_project` — #KB-refactor / #220) + manifest UI + the `editor` and `review` page specs. |
| `pages.py` | The `editor` page surface (#130): the knowledge-base switcher + scopes (#KB-refactor), document/folder tree, read, save, folder CRUD (create, delete, move — #216), and `create_project` (new knowledge base) + the read-only `__docs__` platform-docs scope. `VaultPages` **reads** through a `VaultReader` (the file API by default — #346/ADR-0070; a `DiskVaultReader` for the bundled `__docs__` scope) and **writes** through the core file API (`PlatformClient.files_*`, core path `knowledge/<rel>` — #356/ADR-0064); `create_pages_router` registers the HTTP endpoints. `move_item` relocates the file then calls the indexer's `move_path` to keep the ledger + Qdrant in step (#470) — before this fix only the suggestion-approval path re-indexed a move. A `read_only` flag (watch mode, #232) makes the page view-only and 409s every write. Each save snapshots a version via the injected `VersionStore`, and `list_versions`/`get_version` back the version-history endpoints (#ADR-0046). |
| `suggestions.py` | The `review` page surface (#220, ADR-0033; edit-before-approve + audit, ADR-0090): the `knowledge_suggestions` store (with the added `to_path` column), `SuggestionReview` (diff + apply on approve / discard on reject, across create/update/delete/move/mkdir/mkproject; approve takes optional edited `content`), the `knowledge_suggestion_decisions` audit store (`SuggestionAuditStore`, capped at `MAX_DECISIONS`), and `create_review_router`. The shared wire contract (`ReviewSuggestion`/`ReviewData`/`ApplyResult`/`ApproveBody`/`ReviewDecision`/`ReviewAuditData`) is imported from `epicurus_core.review`, not locally redefined. The review diff reads the current content through a `VaultReader` (#346/ADR-0070); apply writes through the core file API like the editor save (#356/ADR-0064). Approve/reject are operator-only — never MCP tools; `read_only` (watch mode, #232) 409s approve. |
| `reader.py` | The vault **read** seam (#346, ADR-0070): `VaultReader` (`list_dir`/`read_text`/`stat` + the shared recursive `projects`/`tree`/`md_entries` walks) with two backends — `ApiVaultReader` (the default: reads through `PlatformClient.files_*`, no `/data` mount) and `DiskVaultReader` (the bundled `/docs`, and the watch-mode vault). Every read site consumes this instead of the filesystem. |
| `refs.py` | Opaque document refs (base64url `source:path`) + path-safety boundaries: `safe_relative`/`safe_dir_relative`/`safe_project` (resolve against a real root — used by the disk reader + write path) and the filesystem-independent `safe_vault_rel`/`safe_vault_dir_rel` (validate a vault-relative `.md` / dir path and return clean posix — used by the read sites over the file API). |
| `attachments.py` | The attachment source (#137): vault-doc picker + resolve (`VaultAttachments`), reading through the `VaultReader`. |
| `resolver.py` | The hover-card resolver (#143): a cited vault note or platform doc → a `HoverCard` (`KnowledgeResolver`), reading content through a `VaultReader` (vault + docs). |
| `module_docs.py` | `ModuleDocLedger` (Postgres tracking for module-contributed docs) + `ModuleDocsIndexer` (HTTP-based diff/embed/upsert for module docs, #215). |
| `app.py` | Lifespan, `GET /status`, the `/pages/*` (review router first, then editor) + `/attachments/*` + `/resolve/*` + `/module-docs` routers; builds the vault `VaultReader` — an `ApiVaultReader` by default, a `DiskVaultReader` under `VAULT_WATCH` (#346/ADR-0070) — plus a `DiskVaultReader` for the bundled docs, and threads them into the indexer, pages, suggestions, attachments, resolver, and agent tools; launches the background `IndexRunner` (#230) so startup never blocks on the first index, and the `VaultWatcher` (#232) when `VAULT_WATCH` is set. |
| `settings.py` | `KnowledgeSettings` (adds `vault_path`, `docs_path`, Qdrant, DB, platform URL, and the `VAULT_WATCH`/`VAULT_WATCH_DEBOUNCE_MS` watch fields + the derived `vault_read_only`). |
