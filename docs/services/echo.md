# echo — the reference module

**`epicurus-echo`** is the simplest possible module: it exercises **both halves** of the
module↔core contract — an agent-facing MCP tool and the NATS request/reply path — which
makes it the contract proof and the reference a new module is modeled on. Host port
**8080**.

## The contract it exposes

### MCP tool (agent-facing)

| Tool | Purpose |
| --- | --- |
| `echo(message)` | Return the given message unchanged. |

### Events (NATS)

**Consumes** `<tenant>.echo.request` — a request/reply responder that echoes the payload
back. This proves the async event path alongside the synchronous tool.

### Web UI (manifest)

A summary, a `greeting` config field, and a **Send an echo** action — the minimal example
of a manifest-driven module UI (ADR-0007). echo also declares an **Echoes** left-nav page
(`browser` archetype) — the reference for the core-rendered page vocabulary (ADR-0018):
the module supplies only data, the shell renders it.

### HTTP

`GET /health` · `GET /metrics` · `GET /manifest` · `GET /pages/{id}` (page data the core
proxies — `echoes` here) · `/mcp` (the streamable MCP endpoint).

## Configuration

Declares one config key, `greeting`. Otherwise just the shared
[`CoreSettings`](../reference/config.md) (`NATS_URL`, `DEFAULT_TENANT_ID`, …).

## Data model

None — echo is stateless.

## Dependencies

NATS (the request/reply responder). The core calls its tool over MCP and reads its
manifest.

## Run & extend

echo comes up with the default stack. It is the canonical example in
[Building a module](../developer/building-a-module.md) — `services/echo` is essentially
what the service template generates. Package `epicurus_echo`: `service.py` (the module,
the `echo` tool, and the NATS responder) and `app.py` (ops routes + manifest route + the
mounted MCP app).
