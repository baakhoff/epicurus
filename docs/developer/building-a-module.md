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

## Fetch connected-account tokens (OAuth)

A module that calls a third-party API on the user's behalf (Google Calendar, Gmail,
…) **never holds a client secret or refresh token**. The core owns the OAuth connect
flow and the per-tenant token vault; the module asks for a ready-to-use, auto-refreshed
access token with one `PlatformClient` call:

```python
token = await platform.get_oauth_token("google")   # -> str, raises if not connected
headers = {"Authorization": f"Bearer {token}"}
```

**Always go through `get_oauth_token`.** Do not call `/platform/v1/oauth/{provider}/token`
directly, and do not add your own token method to `PlatformClient` — every module shares
this one contract so the credential boundary stays in the core (ADR-0010, ADR-0016). The
user connects the account and grants scopes from the web Settings page; provisioning the
provider's client credentials is an operator step (see
[Secrets](../infrastructure/secrets.md)).

## Scaffold and wire it in

One command generates the module **and** performs every wire-in step, so the new
module passes `task smoke` with no manual edits:

```bash
task new-module -- "My Module"
# or: uv run python scripts/new_module.py "My Module"
```

It renders the template (package, Dockerfile, compose fragment, tests), assigns
the next free host port from the [host-port registry](../reference/ports.md), and
wires the module into the root `pyproject.toml` (mypy + ruff), the top-level
`compose.yaml` `include:` list, the core's `module_urls`, and the smoke CI override
(`infra/ci/compose.ci.yaml`) — then refreshes `uv.lock`. Replace the sample `ping`
tool and run the gates:

```bash
uv sync --all-packages
uv run pytest services/<slug>
task smoke
```

### Wiring it by hand

If you scaffold with bare `cookiecutter templates/service-template` instead, do
these four steps yourself — the **runtime smoke gate** (`task smoke`, the CI
`runtime-smoke` job) boots the stack and fails if a module is present but the
agent can't discover it, so none can be silently skipped:

1. **Register the package** in the root `pyproject.toml`: add it to
   `[tool.mypy] packages` and `[tool.ruff.lint.isort] known-first-party`.
2. **Include the fragment** in the top-level `compose.yaml` `include:` list — the
   gate derives the module set from this list, so your module is gated once it's here.
3. **Register the URL in the core** — add `http://<slug>:8080` to `module_urls` in
   `services/core-app/src/epicurus_core_app/settings.py`. Skip this and the agent
   never sees the module; it is the most-forgotten step, and the gate catches it.
4. **Pick a unique host port** in the fragment from the
   [registry](../reference/ports.md) — the gate flags duplicates.
5. **Reset its port in the smoke override** — add the service with
   `ports: !reset []` to `infra/ci/compose.ci.yaml`, or the smoke stack leaks its
   host binding and collides with a running dev stack.

See [Testing › Runtime smoke gate](testing.md#runtime-smoke-gate) for what it
checks and how to run it locally.

## Conventions

- **Don't call language models directly.** Use `PlatformClient` — it owns the
  model keys, routing, and usage accounting.
- **Fetch secrets from OpenBao at runtime**, never from env files or git.
- **Fetch connected-account tokens via `PlatformClient.get_oauth_token`** — never the
  OAuth endpoint directly, never a bespoke client method. One contract, owned by the core.
- **Keep the module stateless**; put state in the data-plane services.
