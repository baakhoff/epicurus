# Services & Modules

epicurus is a **core** surrounded by **sidecar modules** (ADR-0004): each is its own
container, and adding a capability means running one more container that speaks the
contract. This is the map of every running block.

## The blocks

| Block | Kind | Role | Host port | Reference |
| --- | --- | --- | --- | --- |
| **core-app** | core | the brain — agent loop, LLM gateway, cross-chat memory, power states, the platform API, and the MCP host | `8082` | [core-app](../../services/core-app/README.md) |
| **web** | core | the phone-first PWA shell — chat, model manager, power toggle, manifest-driven module UI | `8084` | [web](../../services/web/README.md) |
| **storage** | module | a read-only index of a file tree (list / search / read / download) plus a MinIO object store | `8083` | [storage](../../services/storage/README.md) |
| **knowledge** | module | Obsidian-vault RAG — incremental index into Qdrant + a `knowledge_search` tool | `8085` | [knowledge](../../services/knowledge/README.md) |
| **echo** | module | the reference module — proves the MCP tool + NATS event contract end to end | `8080` | [echo](../../services/echo/README.md) |

Ports are loopback-bound by default; the [edge gateway](../../infra/edge/README.md) is the
front door at `http://<host>:8088/`.

## How a module plugs in

Every module speaks one **local-only** contract (ADR-0004) over the internal Docker
network:

- **MCP tools → the agent.** The module exposes typed tools (e.g. `storage_read`,
  `knowledge_search`); the core is the MCP host that calls them. See
  [modules & manifest](../reference/modules.md).
- **Platform API → the core.** The module calls back for capabilities it must not own —
  inference (embeddings, chat), secrets, events — via the typed `PlatformClient`. **All AI
  goes through the core; modules never hold model keys.** See
  [platform API](../reference/platform-api.md).
- **NATS events ↔ either way** — asynchronous "something happened" messages. See
  [events](../reference/events.md).
- **A manifest** declares the module's identity, tools, events, config, and its
  declarative web UI — the shell renders it with no rebuild.

To build one, start from the [service template](../developer/building-a-module.md).

> Each block above is being expanded into a full reference page under `docs/services/`
> to the documentation standard; for now the Reference column links each module's README.
