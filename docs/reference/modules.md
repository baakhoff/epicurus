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
| `image` | `str \| None` | `None` | container image (for distribution) |
| `tools` | `list[ToolSpec]` | `[]` | exposed tools |
| `events_emitted` | `list[EventSpec]` | `[]` | published subjects |
| `events_consumed` | `list[EventSpec]` | `[]` | subscribed subjects |
| `config` | `list[str]` | `[]` | required config keys |
| `secrets` | `list[str]` | `[]` | required secret names |
| `ui` | `UiSection \| None` | `None` | declarative web-shell UI (ADR-0007 Tier 1) |
| `pages` | `list[PageSpec]` | `[]` | left-nav pages, core-rendered from a bounded vocabulary (ADR-0018) |

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
`browser` (tree/list + detail), `calendar`, `editor` (Obsidian-like doc),
`board` (lists/cards). The shell ships one first-party screen per archetype;
`browser` is implemented today, the rest land with their module pages (Phase 3.8).

**Serving page data.** The module serves each page's data at **`GET /pages/{id}`** in
the archetype's data shape; the core proxies it at
**`GET /platform/v1/modules/{name}/pages/{id}`** (validated against the manifest's
declared pages — 404 otherwise), so the shell never calls a module directly. The
`browser` archetype's data shape is:

```jsonc
{
  "title": "Echoes",              // optional page heading
  "items": [
    { "id": "hello", "title": "hello", "subtitle": "…", "body": "…" }
  ]
}
```

### `CONTRACT_VERSION`
`"0.1"` — the module↔core contract version this release targets.
