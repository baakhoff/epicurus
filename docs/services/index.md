# Services & Modules

epicurus is a **core** surrounded by **sidecar modules** (ADR-0004): each is its own
container, and adding a capability means running one more container that speaks the
contract. This is the map of every running block — each row links its full reference page.

## The blocks

| Block | Kind | Role | Host port | Reference |
| --- | --- | --- | --- | --- |
| **core-app** | core | the brain — agent loop, LLM gateway, cross-chat memory, power states, the platform API, and the MCP host | `8082` | [core-app](core-app.md) |
| **web** | core | the phone-first PWA shell — chat, model manager, power toggle, manifest-driven module UI | `8084` | [web](web.md) |
| **storage** | module | a read-only index of a file tree (list / search / read / download) plus a MinIO object store | `8083` | [storage](storage.md) |
| **knowledge** | module | Obsidian-vault RAG — incremental index into a tenant-scoped Qdrant collection | `8085` | [knowledge](knowledge.md) |
| **websearch** | module | self-hosted web search via SearXNG — no API key required | `8086` | [websearch](websearch.md) |
| **calendar** | module | provider-neutral calendar — events and scheduling (local + Google) | `8087` | [calendar](calendar.md) |
| **mail** | module | provider-agnostic mail — search, read, and send via Gmail (v0.1) | `8090` | [mail](mail.md) |
| **tasks** | module | task management — Google Tasks plus a local store (ADR-0016) | `8091` | [tasks](tasks.md) |
| **echo** | module | the reference module — proves the MCP tool + NATS event contract end to end | `8080` | [echo](echo.md) |

Ports are loopback-bound by default; the [edge gateway](../infrastructure/index.md#edge-gateway)
is the front door at `http://<host>:8088/`. The full allocation (and how a new
module gets a collision-free one) lives in the [host-port registry](../reference/ports.md).

## How a module plugs in

Every module speaks one **local-only** contract (ADR-0004) over the internal Docker
network:

- **MCP tools → the agent.** The module exposes typed tools (e.g. `storage_read`); the
  core is the MCP host that calls them. See [modules & manifest](../reference/modules.md).
- **Platform API → the core.** The module calls back for capabilities it must not own —
  inference (embeddings, chat), secrets, events — via the typed
  [`PlatformClient`](../reference/platform-client.md). **All AI goes through the core;
  modules never hold model keys.**
- **NATS events ↔ either way** — asynchronous "something happened" messages. See
  [events](../reference/events.md).
- **A manifest** declares the module's identity, tools, events, config, and its
  declarative web UI — the shell renders it with no rebuild.

To build one, start from [Building a module](../developer/building-a-module.md).
