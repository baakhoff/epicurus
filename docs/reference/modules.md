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
(Obsidian-like doc), `board` (lists/cards). The shell ships one first-party screen per
archetype; `browser`, `calendar`, `editor`, and `board` are all implemented today.

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
document *list* (content is fetched lazily per document), and it owns two extra,
**editor-only** doc endpoints the core proxies (a non-`editor` page 404s on them):

```jsonc
// GET /pages/{id}  →  the browsable document list
{ "title": "Knowledge", "docs": [ { "id": "a.md", "title": "a", "path": "a.md" } ] }
// GET /pages/{id}/doc?path=<rel>  →  one document's content
{ "path": "a.md", "title": "a", "content": "# A\n…" }
// PUT /pages/{id}/doc?path=<rel>  with { "content": "…" }  →  save
{ "path": "a.md", "indexed": true, "chunk_count": 3 }
```

Proxied at `GET|PUT /platform/v1/modules/{name}/pages/{id}/doc?path=<rel>`. `path` is
module-relative and the module **must** confine it to its own store (reject `..`,
absolute paths, and non-document files) — the editor writes real files, so this is the
trust boundary. The shared core editor component (knowledge's vault page is the first
user, #130) provides the list + markdown source/preview + save; a module supplies only
the data above. The first knowledge implementation re-indexes a saved document so it
stays agent-retrievable.

The `calendar` archetype's data shape is a window of events (the shell renders the month /
week / agenda views and re-fetches as the user navigates):

```jsonc
{
  "title": "Calendar",
  "provider": "local",                              // active provider
  "range": { "start": "2026-06-01T00:00:00+00:00",  // the window actually returned
             "end":   "2026-07-01T00:00:00+00:00" },
  "events": [
    { "id": "e1", "title": "Standup",
      "start": "2026-06-15T09:00:00+00:00",
      "end":   "2026-06-15T09:30:00+00:00",
      "location": "Room 4", "description": "…", "provider": "local" }
  ]
}
```

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
  `{manifest, status, enabled}` — and **includes disabled modules** so the shell can show
  the toggle. The shell omits a disabled module's pages from the nav and its entities from
  the attach menu.
- **Invoking a disabled module's tool** through the core returns **403**; the agent never
  sees the tool in the first place.
- **Tags** — `ModuleManifest.tags` feed the shell's search alongside the name and
  description.
