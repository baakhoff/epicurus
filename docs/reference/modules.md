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
| `async manifest(*, config=None, secrets=None) -> ModuleManifest` | Build the manifest from registered tools + declared events. |
| `http_app() -> starlette.applications.Starlette` | ASGI app serving the tools over streamable HTTP (internal network). |

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

### `ToolSpec`
`name: str` · `description: str = ""` · `input_schema: dict = {}` (JSON Schema).

### `EventSpec`
`subject: str` · `description: str = ""`. `subject` is the **base** subject;
it's tenant-scoped at runtime.

### `CONTRACT_VERSION`
`"0.1"` — the module↔core contract version this release targets.
