# Reference: `modules`

The MCP module base and the manifest. A module exposes **tools** to the agent and
describes itself with a **manifest**.

## `EpicurusModule`

`epicurus_core.module.EpicurusModule` — wraps the MCP SDK's `FastMCP` with epicurus
conventions.

```python
EpicurusModule(
    name: str,
    *,
    version: str = "0.1.0",
    description: str = "",
    instructions: str | None = None,
    image: str | None = None,
    config: list[str] | None = None,
    secrets: list[str] | None = None,
    ui: UiSection | None = None,
    pages: list[PageSpec] | None = None,
    docs_url: str | None = None,
)
```

### Members

| Member | Description |
| --- | --- |
| `name` *(property)* | The module name. |
| `mcp` *(property)* | The underlying `FastMCP` (advanced use / testing). |
| `tool(name=None, description=None)` | Decorator registering a tool; the function signature becomes the tool's typed input schema. |
| `emits(subject, description="") -> None` | Declare a published event subject. |
| `consumes(subject, description="") -> None` | Declare a subscribed subject. |
| `async manifest(*, config=None, secrets=None) -> ModuleManifest` | Build the manifest from registered tools + declared events (args override the constructor's `config`/`secrets`). |
| `http_app() -> starlette.applications.Starlette` | ASGI app serving the tools over streamable HTTP (internal network). |

### `add_manifest_route`

`epicurus_core.add_manifest_route(app: FastAPI, module: EpicurusModule)` — serves the
module's manifest at **`GET /manifest`**. The core's module registry reads this to
surface the module (tools, events, declared UI) to the agent and the web shell; the
service template wires it by default. A module without it still works as a tool
server — it just renders as a bare card in the shell.

### Example

```python
from epicurus_core import EpicurusModule

module = EpicurusModule("greeter", version="1.0.0")

@module.tool()
def greet(name: str) -> str:
    """Greet someone."""
    return f"Hello, {name}!"

module.emits("greeting.sent")
manifest = await module.manifest(secrets=["GREETER_API_KEY"])
app = module.http_app()
```

## Manifest models — `epicurus_core.manifest`

### `ModuleManifest`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | `str` | — | module name |
| `version` | `str` | — | module version |
| `description` | `str` | `""` | one-line description |
| `contract_version` | `str` | `CONTRACT_VERSION` | contract version targeted |
| `tags` | `list[str]` | `[]` | free-text tags for browsing/filtering modules in the shell (#126); the core never routes on them |
| `image` | `str \| None` | `None` | container image (for distribution) |
| `tools` | `list[ToolSpec]` | `[]` | exposed tools |
| `events_emitted` | `list[EventSpec]` | `[]` | published subjects |
| `events_consumed` | `list[EventSpec]` | `[]` | subscribed subjects |
| `config` | `list[str]` | `[]` | required config keys |
| `secrets` | `list[str]` | `[]` | required secret names |
| `ui` | `UiSection \| None` | `None` | declarative web-shell UI (ADR-0007 Tier 1) |
| `pages` | `list[PageSpec]` | `[]` | left-nav pages, core-rendered from a bounded vocabulary (ADR-0018) |
| `resolver` | `bool` | `False` | module serves `GET /resolve/{kind}/{ref_id}` for hover-cards (ADR-0019) |
| `attachable` | `bool` | `False` | module is a chat-attachment source: serves a picker + resolve (ADR-0019) |
| `required_models` | `list[ModelSlot]` | `[]` | model "slots" the operator fills in the shell (#128); the module fetches its choice and passes it to embed/chat |
| `collections` | `CollectionsSpec \| None` | `None` | account/collection model (ADR-0030): the module serves `GET /accounts` and reads its selection via `PlatformClient.get_collections`; the shell renders a connected-accounts section. `CollectionsSpec` = `{noun: str, multi: bool, providers: list[str]}` |
| `docs_url` | `str \| None` | `None` | relative path on the module (e.g. `/module-docs`) returning usage docs the knowledge service auto-indexes (#215); see *Per-module docs* below |

### `ToolSpec`
`name: str` · `description: str = ""` · `input_schema: dict = {}` (JSON Schema).

### `EventSpec`
`subject: str` · `description: str = ""`. `subject` is the **base** subject;
it's tenant-scoped at runtime.

### `UiSection`

The module's declarative UI — the web shell auto-renders it, so installing a module
surfaces its settings/status with **no shell rebuild and no module JS** (ADR-0007).

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `ui_version` | `str` | `"1"` | versions this vocabulary; a shell seeing an unknown version falls back to a plain card |
| `icon` | `str` | `"puzzle"` | a glyph name from the shell's vendored icon set — never a URL or script |
| `summary` | `str` | `""` | one-line blurb shown on the module card |
| `config_schema` | `dict \| None` | `None` | JSON Schema (object) rendered as the module's settings form; values round-trip through the core into OpenBao (`modules/<name>/config`, tenant-scoped) |
| `actions` | `list[UiAction]` | `[]` | buttons that invoke the module's MCP tools through the core |
| `status_url` | `str \| None` | `None` | relative path on the module (e.g. `/status`) returning a flat JSON object of live status fields; proxied by the core at `GET /platform/v1/modules/{name}/status` and displayed in the shell's **Status** panel — the shell never calls the module directly |
| `ui_url` | `str \| None` | `None` | reserved for Tier 2 (module-served page in a sandboxed iframe) — not rendered yet |

### `UiAction`

A button the shell renders; pressing it invokes one of the module's **MCP tools**
through the core. The input form comes from the tool's own `input_schema` — the same
JSON-Schema vocabulary as tool calls, so an action needs no schema of its own.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `tool` | `str` | — | the MCP tool to invoke |
| `label` | `str` | — | button text |
| `description` | `str` | `""` | helper text under the button |
| `intent` | `"default" \| "primary" \| "danger"` | `"default"` | button styling |
| `confirm` | `str \| None` | `None` | confirmation prompt (required for `danger`) |

### `PageSpec` — module-contributed left-nav pages (ADR-0018)

A module may contribute one or more **left-nav pages**, but the **core renders them**
from a bounded set of view archetypes — the module supplies *data only* and names
which archetype presents it. There is **no module-authored HTML/JS/CSS in the shell**,
and a module cannot invent a page type; the vocabulary extends only in core. This is
the model that supersedes ADR-0007's Tier-2 (iframe) idea for first-party modules.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `id` | `str` | — | page id, unique within the module; forms its data path + nav route |
| `title` | `str` | — | left-nav label |
| `archetype` | `PageArchetype` | — | which core view renders it (see below) |
| `icon` | `str` | `"puzzle"` | glyph name from the shell's vendored icon set |
| `nav_order` | `int` | `100` | sort order in the left nav (lower is higher) |
| `capability` | `str \| None` | `None` | reserved gate the shell may check before showing the page (e.g. a connected account) — not yet enforced |

**`PageArchetype`** — the bounded vocabulary (core-owned, extends only in core):
`browser` (tree/list + detail), `calendar` (month / week / agenda), `editor`
(Obsidian-like doc), `board` (lists/cards), `review` (diff approve/reject queue). The
shell ships one first-party screen per archetype; all five are implemented today.

**Serving page data.** The module serves each page's data at **`GET /pages/{id}`** in
the archetype's data shape; the core proxies it at
**`GET /platform/v1/modules/{name}/pages/{id}`** (validated against the manifest's
declared pages — 404 otherwise), so the shell never calls a module directly. Query params
are **forwarded verbatim** to the module, so a parameterized archetype reads from the same
path — e.g. the `calendar` passes `?start=…&end=…`, and the file `browser` passes `?path=`
and `?q=`. The `browser` archetype's data shape is:

```jsonc
{
  "title": "Echoes",              // optional page heading
  "path": "",                     // optional: current directory path (browser navigation)
  "search_enabled": true,         // optional: when true the shell shows a search input
  "items": [
    {
      "id": "hello",
      "title": "hello",
      "subtitle": "a friendly echo",
      "body": "…",               // optional: shown in the detail pane
      "icon": "file",             // optional: glyph name
      "nav_path": "docs",         // optional: set on directories to enable drill-in
      "href": "/platform/v1/modules/storage/download?path=…"  // optional: download URL for files
    }
  ]
}
```

**Download proxy.** The core also serves `GET /platform/v1/modules/{name}/download?path=…`
which proxies to the module's `GET /download?path=…`. This lets the browser download files
through the core without talking to a module directly — the `href` field in a `BrowserItem`
points here.

The `board` archetype's data shape is **columns of cards**, plus declarative
**actions** — board-level and per-card — that mutate through the contract. An action
names one of the module's **MCP tools**, which the shell invokes via the core
(`POST /platform/v1/modules/{name}/tools/{tool}`, validated against the manifest), so a
core-rendered board edits without any module markup. `args` are fixed values merged into
every call; `form: true` opens a [SchemaForm](#) from the tool's own `input_schema`
(narrowed to `fields`, prefilled with `form_values`) before invoking; `confirm` gates a
one-tap call behind a dialog (required when `intent` is `danger`, mirroring `UiAction`).
After a successful call the shell refetches the page.

```jsonc
{
  "title": "Tasks",                                  // optional page heading
  "columns": [
    {
      "id": "today", "title": "Today",
      "cards": [
        {
          "id": "t1", "title": "Buy milk", "subtitle": "2 litres",
          "badges": [{ "label": "2026-06-14", "tone": "accent" }],
          "done": false,
          "actions": [
            { "tool": "tasks_complete", "label": "Complete", "icon": "check",
              "args": { "task_id": "t1" } },
            { "tool": "tasks_update", "label": "Edit", "icon": "pencil", "form": true,
              "fields": ["title", "notes", "due"], "args": { "task_id": "t1" },
              "form_values": { "title": "Buy milk", "notes": "2 litres", "due": "" } }
          ]
        }
      ]
    }
  ],
  "actions": [
    { "tool": "tasks_add", "label": "Add task", "intent": "primary", "icon": "plus",
      "form": true, "fields": ["title", "notes", "due"] }
  ]
}
```

**The `editor` archetype (Obsidian-like docs).** Its `GET /pages/{id}` returns a
document/folder tree (content is fetched lazily per document), and it owns several
**editor-only** endpoints the core proxies (a non-`editor` page 404s on them):

```jsonc
// GET /pages/{id}  →  the browsable document/folder tree
{
  "title": "Knowledge",
  "docs": [
    { "id": "projects", "title": "projects", "path": "projects", "type": "dir" },
    { "id": "projects/a.md", "title": "a", "path": "projects/a.md", "type": "file" },
    { "id": "b.md", "title": "b", "path": "b.md", "type": "file" }
  ],
  "can_create": false,        // true → shell shows "New note" (Notes module)
  "can_manage_files": true    // true → shell shows folder CRUD (Knowledge module, #216)
}
// GET /pages/{id}/doc?path=<rel>  →  one document's content
{ "path": "projects/a.md", "title": "a", "content": "# A\n…" }
// PUT /pages/{id}/doc?path=<rel>  with { "content": "…" }  →  save
{ "path": "projects/a.md", "indexed": true, "chunk_count": 3 }
```

The following additional endpoints are available when `can_manage_files` is true (#216):

```
POST   /pages/{id}/folder?path=<rel>          →  { "path": "…" }   (201 if created, 409 if exists)
DELETE /pages/{id}/doc?path=<rel>             →  204               (404 if absent)
DELETE /pages/{id}/folder?path=<rel>          →  204               (409 if not empty, 404 if absent)
POST   /pages/{id}/move  { from_path, to_path } →  { "path": "…" }  (404 source absent, 409 dest exists)
```

Proxied at:

- `GET|PUT /platform/v1/modules/{name}/pages/{id}/doc?path=<rel>`
- `POST /platform/v1/modules/{name}/pages/{id}/folder?path=<rel>`
- `DELETE /platform/v1/modules/{name}/pages/{id}/doc?path=<rel>`
- `DELETE /platform/v1/modules/{name}/pages/{id}/folder?path=<rel>`
- `POST /platform/v1/modules/{name}/pages/{id}/move`

`path` is module-relative and the module **must** confine it to its own store (reject
`..`, absolute paths, and — for doc paths — non-``.md`` files) — the editor writes real
files, so this is the trust boundary. The shared core editor component (knowledge's vault
page is the first user, #130) provides the tree + markdown source/preview + save; a
module supplies only the data above. The knowledge implementation re-indexes a saved
document so it stays agent-retrievable.

`can_manage_files` tells the shell to show folder CRUD controls (Knowledge sets this
`true`; Notes sets it `false` and uses `can_create` instead for its own authoring flow).
`EditorDoc.type` distinguishes `"file"` entries from `"dir"` entries; the shell builds
the nested visual tree from the flat list using the path structure.

**The `review` archetype (suggested-changes queue, #220).** A queue of agent-proposed
changes the operator approves or rejects, each with a server-computed unified diff. Its
`GET /pages/{id}` returns the pending queue, and it owns two **operator-only** mutation
endpoints the core proxies (they are deliberately *not* MCP tools — the agent could
otherwise approve its own proposals):

```jsonc
// GET /pages/{id}  →  the pending queue
{
  "title": "Suggestions",
  "suggestions": [
    { "id": "9f2c…",                 // opaque suggestion id
      "title": "goals",
      "path": "projects/goals.md",
      "operation": "update",          // create | update | delete
      "origin": "agent",
      "note": "add Q3 targets",       // optional rationale
      "created_at": "2026-06-18T21:30:00+00:00",
      "diff": "--- a/…\n+++ b/…\n@@ …" // unified diff: current vault → proposed
    }
  ]
}
// POST /pages/{id}/suggestions/{sid}/approve  →  applies + indexes, drops the row
{ "id": "9f2c…", "status": "approved", "path": "projects/goals.md",
  "operation": "update", "indexed": true }
// POST /pages/{id}/suggestions/{sid}/reject   →  discards the row, vault untouched
{ "id": "9f2c…", "status": "rejected", "path": "projects/goals.md", "operation": "update" }
```

Proxied at:

- `GET  /platform/v1/modules/{name}/pages/{id}` (the queue — same proxy as any page)
- `POST /platform/v1/modules/{name}/pages/{id}/suggestions/{sid}/approve`
- `POST /platform/v1/modules/{name}/pages/{id}/suggestions/{sid}/reject`

The trust boundary is the **author**: agent-initiated changes (the knowledge
`knowledge_propose_edit` tool) stage a suggestion and land only on approval; direct
*operator* edits (the editor save, the file-tree CRUD above) stay immediate, since the
operator is the approver. Knowledge is the first user (ADR-0033); see
[knowledge](../services/knowledge.md).

The `calendar` archetype's data shape is a window of events (the shell renders the month /
week / agenda views and re-fetches as the user navigates). Like the `board`, it is
**read-write**: it carries the same declarative **actions** — page-level (e.g. "New event")
and per-event (Edit / Delete) — that name MCP tools the shell invokes through the core's tool
proxy, refetching on success (ADR-0024, #208):

```jsonc
{
  "title": "Calendar",
  "provider": "local",                              // sources present in the window
  "range": { "start": "2026-06-01T00:00:00+00:00",  // the window actually returned
             "end":   "2026-07-01T00:00:00+00:00" },
  "events": [
    { "id": "e1", "title": "Standup",
      "start": "2026-06-15T09:00:00+00:00",
      "end":   "2026-06-15T09:30:00+00:00",
      "location": "Room 4", "description": "…", "provider": "local",
      "actions": [                                  // per-event Edit (form) + Delete (confirm)
        { "tool": "calendar_update_event", "label": "Edit", "icon": "pencil", "form": true,
          "args": { "event_id": "e1" }, "fields": ["title", "start", "end", "location", "description"],
          "form_values": { "title": "Standup", "start": "…", "end": "…" } },
        { "tool": "calendar_delete_event", "label": "Delete", "icon": "trash",
          "intent": "danger", "confirm": "Delete 'Standup'?", "args": { "event_id": "e1" } }
      ] }
  ],
  "actions": [                                       // page-level "New event"
    { "tool": "calendar_create_event", "label": "New event", "icon": "plus", "intent": "primary",
      "form": true, "fields": ["title", "start", "end", "location", "description"],
      "form_values": { "start": "…", "end": "…" } }
  ]
}
```

A tool field whose JSON-Schema declares `format: "date-time"` (or `"date"`) is rendered by
the shared form as a native datetime/date picker, and `format: "multiline"` as a textarea —
so the calendar's `start`/`end` get pickers without any custom UI. The same `actions`
vocabulary works for any archetype that wants core-rendered mutations.

### Entity references & the resolver (ADR-0019)

The assistant can mention a module entity (an event, task, email, doc…) as an
**interactive reference** — a chip that shows a hover-card and opens in the right panel.

- **A tool emits references** by returning a JSON `ToolEnvelope` instead of a bare
  string — use `epicurus_core.tool_envelope(text, [EntityRef(...)])`. The agent feeds
  `text` back to the model and lifts the refs onto the turn (persisted on the message).
  Tools that return plain strings are unaffected.
- **`EntityRef`** = `ref_id` · `module` · `kind` · `title` · `summary?` — enough to
  render the chip immediately.
- **The hover-card** is fetched on demand from the module's **resolver**: declare
  `resolver=True` and serve `GET /resolve/{kind}/{ref_id}` returning a **`HoverCard`**
  (`title` · `description` · `details: [{label, value}]` · `href?: {label, url}`). The
  core proxies it at `GET /platform/v1/modules/{name}/resolve/{kind}/{ref_id}`.

This is the uniform, core-owned shape for every entity (it also backs the panel's
`entity-detail` view); modules supply data only, never markup.

### Attachment sources (ADR-0019)

A module can be a **chat-attachment source** so its entities can be attached to a turn.
Declare `attachable=True` and serve two endpoints (the core proxies both):

- **Picker** — `GET /attachments` → a list of `{ref_id, kind, title}` the composer lists
  (proxied at `GET /platform/v1/modules/{name}/attachments`).
- **Resolve** — `GET /attachments/{ref_id}` → `{title, excerpt}` (or `text`); the agent
  injects the excerpt into the turn's context.

The user can also attach an uploaded **file** (held core-side, `POST /platform/v1/agent/attachments`)
or another **chat** (by session id) — those need no module. The agent expands every
attachment into context at turn time. An uploaded file is **additionally** persisted to
the storage module's object store (the upload sink, ADR-0025) so it is kept durably and
becomes browsable in the Files page — best-effort, so a down storage never fails the
upload. See [storage](../services/storage.md#the-chat-upload-sink-adr-0025).

### `CONTRACT_VERSION`
`"0.1"` — the module↔core contract version this release targets.

## Enabling, disabling & browsing modules (#126)

The operator can turn a module **on or off** from the shell's Modules screen and find
modules by name, description, or tag. The flag is a **core-side registry preference**
(persisted per tenant in Postgres — the `module_prefs` table), so the module's
**container keeps running**: disabling never touches Docker. (Removing the container
is a separate, privileged action — see issue #127.)

- **Disabling hides the module** from the agent's tools (it is dropped from MCP tool
  discovery), the **left-nav pages**, and the chat attach menu — while it stays listed on
  the Modules screen with a re-enable toggle. Re-enabling restores everything.
- **Endpoint** — `POST /platform/v1/modules/{name}/enabled` with body `{ "enabled": bool }`
  persists the choice (404 for an unknown module).
- **The module list** (`GET /platform/v1/modules`) carries the flag on each snapshot —
  `{manifest, status, enabled, disabled_tools}` — and **includes disabled modules** so the
  shell can show the toggle. The shell omits a disabled module's pages from the nav and its
  entities from the attach menu.
- **Invoking a disabled module's tool** through the core returns **403**; the agent never
  sees the tool in the first place.
- **Tags** — `ModuleManifest.tags` feed the shell's search alongside the name and
  description.

## Per-tool enable/disable (#213)

Within an enabled module the operator can turn off **individual tools** — so, for example,
a module's destructive tools are hidden from the agent while its read-only ones remain
available. The module keeps running and other tools are unaffected.

- **Endpoint** — `POST /platform/v1/modules/{name}/tools/{tool}/enabled` with body
  `{ "enabled": bool }`. **404** unknown module or tool not declared in the manifest.
- **Persisted** in `module_prefs.disabled_tools` (a JSON list of disabled tool names per
  `(tenant, module)` row). A tool absent from the list is enabled by default.
- **The agent never sees a disabled tool.** `McpHost.discover` filters disabled tools
  from the tool list offered to the LLM; the same filtering applies to the `route` map so
  a call to a filtered-out tool would return `error: unknown tool` (the model should never
  reach this path).
- **The module list snapshot** includes `disabled_tools: list[str]` on each
  `ModuleSnapshot`; the shell renders each declared tool as a toggleable row — a strikethrough
  style indicates disabled, and the toggle invokes the endpoint above.
- **Re-enabling** removes the tool from `disabled_tools` and it reappears immediately in
  the next `discover` call (no restart needed).

## Removing a module — confirmed container delete (#127, ADR-0028)

Beyond disabling, the operator can **delete** a module's container from the Modules screen
("Danger zone → Remove module"), gated by a confirm dialog. This is a **privileged** action:
the core stops and removes the container through the Docker socket.

- **Endpoint** — `DELETE /platform/v1/modules/{name}` stops + removes the module's container
  and **tombstones** the module (a `removed` flag on `module_prefs`). **404** unknown module ·
  **403** protected service · **503** when the core has no Docker access.
- **Tightly scoped (security).** The core reaches Docker only through one `DockerController`,
  which removes **only a configured module's own container** — matched by both its
  `com.docker.compose.service` **and** `com.docker.compose.project` labels, so a co-located
  stack is never touched — and **never** core-app, web, or a data-plane / infra service (a
  hard denylist on top of the configured-module guard). The read-write socket is mounted on
  `core-app` only; drop that mount to disable removal entirely (the endpoint then 503s).
- **It stays gone.** A removed module is dropped from the module list, agent tool discovery,
  and the nav. Because a `compose up` / Watchtower pull could recreate the container, the core
  **re-removes** any tombstoned module whose container reappears, on every startup. Bringing a
  module back means redeploying it and clearing its tombstone.

See ADR-0028 for the full rationale and security posture.

## Per-module model selection (#128, ADR-0029)

A module can let the operator pick which model fills a named **slot** — e.g. knowledge's
embedding model, independent of the chat default.

- **Declare slots** in the manifest: `required_models: list[ModelSlot]`, where
  `ModelSlot = {key, role: "embedding" | "chat", label, description?}`. The shell renders a
  model picker per slot (in the module's card); the core never routes on slots.
- **The core stores the choice; the module reads it and passes it.** Selections persist in
  `module_prefs.models` (`{slot_key: model_id}`), set via
  `PUT /platform/v1/modules/{name}/models` (`{"models": {...}}`; an unknown slot key is `400`).
  A module resolves its slot with **`PlatformClient.get_module_model(slot)`** (construct the
  client with `module=<name>`) → the chosen model id or `None`, and passes it to `embed` /
  `chat`. `GET …/models/{slot}` backs the helper; `GET …/models` returns the full
  `{slot: model}` map for the shell.
- **Unset = core default.** A blank pick clears the slot; an unset slot (or a module that
  never calls the helper) falls back to the core's configured default. `/embed` and `/chat`
  are unchanged — per-module selection rides their existing explicit-`model` override (ADR-0021).

See ADR-0029 for the rationale (why the module passes the model rather than the core resolving
it by identity).

## Per-module docs contribution (#215)

A module can contribute usage documentation that the knowledge service auto-indexes into the
shared `<tenant>__docs` Qdrant collection, alongside the platform's own bundled docs. This
means the agent can retrieve a module's how-to content with no operator action.

**Declare `docs_url`** in the manifest (e.g. `docs_url="/module-docs"` — not `/docs`, which is
FastAPI's built-in Swagger UI). Serve a JSON response at that path:

```jsonc
// GET /module-docs
{
  "documents": [
    { "path": "usage.md",  "content": "# Using the Calendar\n…" },
    { "path": "tools.md",  "content": "# Available tools\n…" }
  ]
}
```

`path` is a relative identifier (used for display and incremental diffing); `content` is the
raw markdown. The core proxies the endpoint at **`GET /platform/v1/modules/{name}/docs`** —
the knowledge service fetches from there, never from the module directly.

**Indexing behaviour.** The knowledge service calls `GET /platform/v1/modules` on startup to
discover active modules, fetches each module's docs, diffs by SHA-256 content hash, and upserts
only new or changed documents into `<tenant>__docs` with a `module/<name>/` path prefix so they
don't collide with platform docs. Modules that are **disabled or removed** have their docs purged
from the collection automatically. The `knowledge_reindex` tool repeats this process on demand.

**The module docs are automatically searched.** Because they land in `<tenant>__docs`, the
existing `knowledge_search` tool finds them alongside platform docs — no change to the tool
or its callers is needed.

**Tracking table.** The knowledge service records each indexed module doc in
`knowledge_module_docs` (see [knowledge service docs](../services/knowledge.md#data-model)).

A module with no docs to share omits `docs_url` (the default `None`); that module is ignored
by the indexer.
