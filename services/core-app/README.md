# Core runtime (`epicurus-core-app`)

The epicurus **core runtime** — the brain the rest of the platform is built on. It
hosts (incrementally, across Phase 1) the agent loop, the LLM gateway, cross-chat
memory, the power-state machine, and the **MCP host** that drives modules' tools, and
it serves the module-facing **platform API**.

> Built on the `epicurus-core` library. Unlike a sidecar **module** (which exposes
> MCP tools *to* the agent), the core is the **host**: it is the agent that calls
> modules, and the platform other services depend on (ADR-0004 / ADR-0009). So it
> serves a platform API rather than mounting its own MCP tool server.

This skeleton stands the service up:

- `GET /health` + `GET /metrics` — the ops surface every service exposes.
- A connected **NATS** event bus for the process lifetime.
- `GET /platform/v1/info` — the first slice of the platform API.

The agent loop, LLM gateway, memory, and power states land with their Phase-1 cards.

## Develop

```bash
uv sync --all-packages
uv run pytest services/core-app
```

## Run in the stack

It is wired into the top-level `compose.yaml`, so it comes up with the stack
(`docker compose up -d`) — or on its own:

```bash
docker compose up -d core-app
```

Routed by the edge gateway at `core-app.localhost`; reachable directly (loopback) on
`${CORE_PORT:-8082}`.
