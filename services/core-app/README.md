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
  - `POST /platform/v1/llm/chat` — a completion for a list of messages. `model` is
    `<provider>/<model>` (e.g. `claude/claude-3-5-sonnet-latest`); a bare name
    (e.g. `llama3.2`) targets local Ollama.
  - `GET /platform/v1/llm/models` · `POST /platform/v1/llm/pull` — list / fetch local models.
  - `GET /platform/v1/llm/providers` — providers and whether each one's key is set.
  - `GET` + `PUT /platform/v1/power` — the main-page power toggle (ADR-0005):
    `paused` unloads models and refuses inference (`503`); `idle` resumes.
- The **agent** (ADR-0001) — a thin tool-calling loop:
  - `POST /platform/v1/agent/chat` — runs a turn: offers the modules' tools to the
    LLM, runs any tool calls over MCP, feeds the results back, and loops to an answer
    (`AGENT_MAX_STEPS`, default 4). The core is the **MCP host**; the modules it
    discovers tools from are set by `MCP_MODULE_URLS` (default the echo module).

Cross-chat memory and the web UI shell land with their later Phase-1 cards (#39–#40).

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
