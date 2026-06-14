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
- The **LLM gateway** (ADR-0010), via LiteLLM over local **Ollama** *and* hosted
  providers (Claude, ChatGPT, Grok, DeepSeek, Gemini, and a generic
  OpenAI-compatible "any LLM"):
  - Chat completions go through `POST /platform/v1/chat` — the single module-facing
    chat path (ADR-0021). `model` is `<provider>/<model>` (e.g.
    `claude/claude-3-5-sonnet-latest`); a bare name (e.g. `llama3.2`) targets local
    Ollama. (The gateway's own `/platform/v1/llm/chat` was removed in 0.2.0.)
  - `GET /platform/v1/llm/models` · `POST /platform/v1/llm/pull` — list / fetch local
    models; `POST /platform/v1/llm/pull/stream` streams pull progress as SSE;
    `DELETE /platform/v1/llm/models?name=…` removes one.
  - `GET /platform/v1/llm/providers` — providers and whether each one's key is set;
    `PUT` / `DELETE /platform/v1/llm/providers/{alias}/key` stores / clears a hosted
    provider's API key (core → OpenBao; never logged, never returned).
  - `GET` + `PUT /platform/v1/power` — the main-page power toggle (ADR-0005):
    `paused` unloads models and refuses inference (`503`); `idle` resumes.
- The **agent** (ADR-0001) — a thin tool-calling loop:
  - `POST /platform/v1/agent/chat` — runs a turn: offers the modules' tools to the
    LLM, runs any tool calls over MCP, feeds the results back, and loops to an answer
    (`AGENT_MAX_STEPS`, default 4). The core is the **MCP host**; the modules it
    discovers tools from are set by `MODULE_URLS` (default the echo module).
  - `POST /platform/v1/agent/chat/stream` — the same turn as **SSE**: `delta`
    (content tokens), `tool` (a tool call ran), `done` (the final `AgentTurn`),
    `error`. This is what the web shell's chat speaks.
  - `GET /platform/v1/agent/sessions` · `GET /platform/v1/agent/sessions/{id}` ·
    `DELETE /platform/v1/agent/sessions/{id}` — list conversations (last snippet +
    timestamps), fetch one's messages, or forget one (rows + recall vectors).
- The **module registry** (ADR-0004/0007) — what the web shell renders:
  - `GET /platform/v1/modules` — every configured module: its **manifest** (tools,
    events, declared UI) fetched from the module's `GET /manifest`, plus live health.
  - `GET` / `PUT /platform/v1/modules/{name}/config` — the module's config values,
    stored tenant-scoped in OpenBao (`modules/<name>/config`).
  - `POST /platform/v1/modules/{name}/tools/{tool}` — invoke a manifest-declared UI
    action (runs the module's MCP tool through the core's MCP host).
- **Cross-chat memory** — pass a `session_id` to `POST /platform/v1/agent/chat` and the
  turn is grounded in that session's prior messages **plus** semantically recalled
  snippets from earlier conversations (same tenant); the new input and the answer are
  then persisted. History lives in **Postgres**; recall is **Qdrant** over embeddings
  from a local model. Memory is best-effort — if the store, vector DB, or embedder is
  down, the turn still answers, just without memory. Omit `session_id` for a stateless turn.

The [web shell](../web/) consumes all of this — it is the human face of the platform.

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

Hosted-provider API keys live in **OpenBao**, never in env or git: store
`{"api_key": ...}` (plus `api_base` for `custom`) at `tenants/<tenant>/llm/<provider>`
(e.g. `llm/anthropic`, `llm/openai`, `llm/google`). The gateway fetches them per
request and never logs them.

**Routing & usage.** A request tries the chosen model, then `LLM_FALLBACKS` (a
comma-separated chain) on failure; while paused, local models are skipped but a
hosted fallback still serves (ADR-0005). Retries on 429/5xx use LiteLLM's backoff
(`LLM_NUM_RETRIES`, default 2). Every call emits a usage event on NATS
(`<tenant>.llm.usage`: model, tokens, latency — no prompt content, no keys).

**Memory.** Conversation history is persisted to **Postgres** (`DATABASE_URL`) and
indexed for semantic recall in **Qdrant** (`QDRANT_URL`), embedded with a local model
(`MEMORY_EMBED_MODEL`, default `nomic-embed-text`, pulled at runtime like any other).
Recall is tenant-scoped — one Qdrant collection (`<tenant>__memory`) per tenant. Both
default to the stack's data-plane services; memory is opt-in per request via `session_id`.
