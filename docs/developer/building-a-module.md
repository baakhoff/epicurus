# Building a module

A module is a small service that exposes **tools** the agent can call and,
optionally, reacts to **events**. You build it with the `epicurus-core` library.

This page uses the building blocks that exist today.

## Define tools

`EpicurusModule` wraps the MCP server. Register tools with the `@tool` decorator —
the function signature becomes the tool's typed input schema:

```python
from epicurus_core import EpicurusModule

module = EpicurusModule(
    "greeter",
    version="0.1.0",
    description="Greets people",
)

@module.tool()
def greet(name: str) -> str:
    """Greet someone by name."""
    return f"Hello, {name}!"
```

## Declare events

Declare the event subjects the module publishes or subscribes to. Subjects are
**tenant-scoped** automatically (`<tenant>.<subject>`):

```python
module.emits("greeting.sent", "published after a greeting")
module.consumes("inbox.message", "incoming chat messages")
```

## Generate the manifest

The manifest is built from the registered tools and declared events. List the
config keys and secret names the module needs:

```python
manifest = await module.manifest(secrets=["GREETER_API_KEY"])
# manifest.tools -> [ToolSpec(name="greet", ...)]
# manifest.events_emitted -> [EventSpec(subject="greeting.sent", ...)]
```

## Serve over HTTP

A module serves its tools over the internal Docker network using the
streamable-HTTP transport:

```python
app = module.http_app()   # a Starlette ASGI app

# run it, e.g. with uvicorn:
#   uvicorn yourmodule:app --host 0.0.0.0 --port 8080
```

## Publish and consume events

Use `EventBus` for NATS events. It connects to the core's NATS and scopes
subjects by tenant:

```python
from epicurus_core import Event, EventBus


async def on_message(event: Event) -> None:
    print(event.json())


async with EventBus("nats://nats:4222") as bus:
    await bus.subscribe("inbox.message", on_message, tenant_id="local")
    await bus.publish("greeting.sent", {"name": "Ada"}, tenant_id="local")
```

`request` / `reply` are available for synchronous request/response over NATS.

## Configuration, logging, health

Reuse the shared building blocks rather than re-implementing them:

```python
from epicurus_core import CoreSettings, add_ops_routes, configure_logging, get_logger

settings = CoreSettings()
configure_logging(settings)
log = get_logger(__name__)

# mount GET /health and GET /metrics on a FastAPI app:
# add_ops_routes(fastapi_app, service_name="greeter")
```

## Call the LLM gateway via `PlatformClient`

Modules must never call a language model directly or hold provider API keys.
Use `PlatformClient` (ADR-0004, ADR-0010) — it proxies through the core's LLM
gateway and keeps all secrets in the core.

```python
from epicurus_core import CoreSettings, PlatformClient, PlatformMessage

settings = CoreSettings()
platform = PlatformClient(
    base_url=settings.platform_url,      # http://core:8080 on the Docker network
    tenant_id=settings.default_tenant_id,
)

# Embed texts — useful for RAG indexing, semantic search, etc.
vectors = await platform.embed(["text to index", "another chunk"])
# -> [[0.023, -0.117, ...], [0.089, 0.042, ...]]

# Chat completion — the core routes, falls back, and meters usage
result = await platform.chat(
    [PlatformMessage(role="user", content="summarise this document")],
    model="claude/claude-3-5-sonnet-latest",  # optional override
)
print(result.content)
```

- The core's configured embedding model is used when `model` is omitted from
  `embed()`.
- The core handles fallback chains (local → hosted) and power-state checks
  (ADR-0005).
- A usage event is emitted on NATS after every call — no prompt content,
  no keys.

See [Platform API reference](../reference/platform-api.md) for the full HTTP
contract and `PlatformChatResponse` type.

## Conventions

- **Don't call language models directly.** Use `PlatformClient` — it owns the
  model keys, routing, and usage accounting.
- **Fetch secrets from OpenBao at runtime**, never from env files or git.
- **Keep the module stateless**; put state in the data-plane services.
