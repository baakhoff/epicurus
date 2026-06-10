# Core runtime (`epicurus-core-app`)

The epicurus **core runtime** — the brain the rest of the platform is built on. It
hosts (incrementally, across Phase 1) the agent loop, the LLM gateway, cross-chat
memory, the power-state machine, and the **MCP host** that drives modules' tools, and
it serves the module-facing **platform API**.

> Built on the `epicurus-core` library. Unlike a sidecar **module** (which exposes
> MCP tools *to* the agent), the core is the **host**: it is the agent that calls
> modules, and the platform other services depend on (ADR-0004 / ADR-0009). So it
> serves a platform API rather than mounting its own MCP tool server.

What it serves today:

- `GET /health` + `GET /metrics` — the ops surface every service exposes.
- A connected **NATS** event bus for the process lifetime.
- `GET /platform/v1/info` — the platform-API discovery surface.
- The **LLM gateway** (ADR-0010), via LiteLLM over a local **Ollama** runtime:
  - `POST /platform/v1/llm/chat` — a completion for a list of messages.
  - `GET /platform/v1/llm/models` · `POST /platform/v1/llm/pull` — list / fetch models.
  - `GET` + `PUT /platform/v1/power` — the main-page power toggle (ADR-0005):
    `paused` unloads models and refuses inference (`503`); `idle` resumes.

The agent loop, cross-chat memory, and hosted LLM providers land with their later
Phase-1 cards (#36–#40).

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

The gateway reaches Ollama at `OLLAMA_URL` (default `http://ollama:11434` in the
stack) and defaults to the `LLM_DEFAULT_MODEL` model (`llama3.2`). Models are pulled
and managed at runtime via `/platform/v1/llm/pull` — never baked into an image.
See [`infra/ollama`](../../infra/ollama/) (CPU by default, GPU opt-in).
