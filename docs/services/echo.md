# echo — the reference module

**`epicurus-echo`** is the simplest possible module: it exercises **every half** of the
module↔core contract — an agent-facing MCP tool, the NATS request/reply path, and the
module event spine — which makes it the contract proof and the reference a new module is
modeled on. Host port **8080**.

## The contract it exposes

### MCP tools (agent-facing)

| Tool | Purpose | `side_effect` |
| --- | --- | --- |
| `echo(message)` | Return the given message unchanged. | `read` |
| `echo_ping(note="", dedup_key="")` | Announce an `echo.pinged` event on the module event spine. Returns the dedup key it was filed under. | `write` |

`side_effect` classifies a tool for the [automations](../reference/automations.md#tool-side-effects)
autonomy dial (ADR-0105), and echo is the reference for that too: `echo` observes and changes
nothing, while `echo_ping` puts an event on the bus that other things react to. Unannotated
would mean `write` for both — safe, but it would leave a Notify automation with no echo tool
at all, which is exactly what annotating a read tool buys you.

### Events (NATS)

**Consumes** `<tenant>.echo.request` — a request/reply responder that echoes the payload
back. This proves the async event path alongside the synchronous tool.

**Emits** `<tenant>.events.echo.pinged` — the [event spine](../reference/events.md)'s
reference emitter (ADR-0103), and the smallest real event there is, so emit → intake →
durable log → feed has something to prove itself against on a fresh stack (the smoke gate
asserts exactly that chain). Fired by the `echo_ping` tool or the **Ping the spine** UI
action.

Two things it demonstrates deliberately:

- **The payload is a pointer, not content** — an optional `note`, truncated to 200 chars.
  echo models the discipline the envelope enforces. It never repeats an envelope field:
  `dedup_key` is already carried above, and a payload key of that name would trip the
  credential screen (`key`).
- **`dedup_key` behaves both ways.** Omit it and each ping is its own event — a fresh uuid
  is *correct* here, and echo is the one place that is true, because two pings genuinely
  are two changes. Every other emitter reports a change it may re-see and must derive its
  key from that change. Pass one explicitly and two pings collapse to a single logged
  event — which is how the demo surface (and the smoke gate) proves the log's idempotency.

The event carries an `EntityRef` of kind `ping`, so the raw events feed renders it as a
hover-card chip with no echo-specific code in the shell (ADR-0019).

### Web UI (manifest)

A summary, a `greeting` config field, and two actions — **Send an echo** and **Ping the
spine** — the minimal example of a manifest-driven module UI (ADR-0007). echo also declares an **Echoes** left-nav page
(`browser` archetype) — the reference for the core-rendered page vocabulary (ADR-0018):
the module supplies only data, the shell renders it.

### Automation template (manifest)

echo declares one preset automation — *"Tell me when the spine is pinged"*, a Notify-level
turn triggered by `echo.pinged` — the reference for the
[Templates](../reference/automations.md#templates) contract (ADR-0105). Declaring it creates
**nothing**: the operator instantiates it from the Templates tab, so installing echo never
makes the assistant start doing anything on its own.

### Resolver (manifest)

echo declares `resolver=True` and serves `GET /resolve/{kind}/{ref_id}`, returning the
uniform hover-card envelope (ADR-0019) — the reference for resolving a chat entity reference.

### HTTP

`GET /health` · `GET /metrics` · `GET /manifest` · `GET /pages/{id}` (page data the core
proxies — `echoes` here) · `GET /resolve/{kind}/{ref_id}` (hover-card resolver) · `/mcp`
(the streamable MCP endpoint).

## Configuration

Declares one config key, `greeting`. Otherwise just the shared
[`CoreSettings`](../reference/config.md) (`NATS_URL`, `DEFAULT_TENANT_ID`, …), including
optional OpenTelemetry tracing (`OTEL_TRACES_ENABLED`, off by default) — echo is the
reference module that wires [`setup_tracing`](../reference/observability.md#tracing-57-adr-0068).

## Data model

None — echo is stateless.

## Dependencies

NATS (the request/reply responder, and the spine emitter). The core calls its tools over
MCP, reads its manifest, and records its events.

## Run & extend

echo comes up with the default stack. It is the canonical example in
[Building a module](../developer/building-a-module.md) — `services/echo` is essentially
what the service template generates. Package `epicurus_echo`: `service.py` (the module,
the `echo` / `echo_ping` tools, the NATS responder, and the `emit_ping` spine emitter) and
`app.py` (ops routes + manifest route + the mounted MCP app).

`build_module(bus=None, *, tenant="local")` takes the bus its ping tool emits on. It is
optional so a caller that only wants the manifest — tests, or the installer reading a
module's descriptor — can build one without standing up NATS; the ping tool then reports
the spine as unavailable rather than the build failing.
