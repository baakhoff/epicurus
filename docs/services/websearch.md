# websearch

Self-hosted web search for the agent.  The websearch module runs a
[SearXNG](https://docs.searxng.org/) instance inside the stack and exposes a
single `web_search` MCP tool.  No external API keys are required.

## What it is

The module adds two containers to the stack:

- **SearXNG** (`infra/searxng/`) — a privacy-preserving metasearch engine.
  Internal-only: reachable at `http://searxng:8080` on the Docker network; no
  host port is published by default.
- **websearch** (`services/websearch/`) — a FastAPI service that wraps SearXNG
  with the standard epicurus module contract (MCP, manifest, health, metrics).

## Contract

### MCP tools

| Tool | Description |
| ---- | ----------- |
| `web_search(query, num_results?)` | Search the web for `query`; returns up to `num_results` results (default: configured max, capped at 20). |

#### `web_search` return type

Each element in the returned list is:

```json
{
  "title":   "Page title",
  "url":     "https://example.com",
  "snippet": "Brief description from the search result",
  "engine":  "google"
}
```

Results are ordered by SearXNG's relevance ranking.  An empty list is returned
when SearXNG finds nothing or is temporarily unreachable.

### HTTP endpoints

| Method | Path | Description |
| ------ | ---- | ----------- |
| `GET` | `/health` | Liveness probe (standard epicurus health response). |
| `GET` | `/metrics` | Prometheus metrics. |
| `GET` | `/manifest` | Module manifest (tools, UI, config schema). |
| `GET` | `/status` | SearXNG reachability: `{"searxng_healthy": true, "searxng_url": "..."}`. |
| `*` | `/mcp/*` | Streamable-HTTP MCP transport (agent connects here). |

### Events

The websearch module emits and consumes no NATS events.

## Configuration

### websearch service

| Environment variable | Default | Description |
| -------------------- | ------- | ----------- |
| `SEARXNG_URL` | `http://searxng:8080` | Base URL of SearXNG on the internal network. |
| `PLATFORM_URL` | `http://core-app:8080` | Core platform API (reserved for future LLM use). |
| `WEBSEARCH_MAX_RESULTS` | `5` | Default maximum results per search (operator override). |
| `WEBSEARCH_ENGINES` | _(empty)_ | Comma-separated SearXNG engine names. Empty = SearXNG defaults. |
| `NATS_URL` | `nats://nats:4222` | NATS connection string. |
| `DEFAULT_TENANT_ID` | `local` | Tenant context. |
| `WEBSEARCH_PORT` | `8086` | Host port the module is published on (dev only). |

### SearXNG

SearXNG is configured via `infra/searxng/settings.yml`.  The defaults ship
with HTML and JSON output formats enabled and rate-limiting disabled (safe for
internal use).  Key settings to review before production:

| Setting | Location | Description |
| ------- | -------- | ----------- |
| `server.secret_key` | `infra/searxng/settings.yml` | Rotate this from the placeholder value. |
| Engines | `infra/searxng/settings.yml` | Uncomment or customise the engine list. |

Override the settings file by setting `SEARXNG_SETTINGS_FILE` in `.env` to
an absolute path on the host.

## Data model

The websearch module holds no persistent state.  It is a stateless proxy
between the agent and SearXNG.  SearXNG itself stores nothing — it fans out
queries to upstream engines on each request.

## Dependencies

| Service | Why |
| ------- | --- |
| SearXNG | The search backend; must be healthy before the module starts. |
| NATS | Event bus (connected at startup; no events are used in v0.1). |
| core-app | Platform API URL wired for future LLM post-processing. |

## Run & extend

### Run locally (development)

Enable the module by ensuring both the searxng infra fragment and the websearch
module fragment are in the root `compose.yaml` include list (they are by
default).  Then:

```sh
task up
# or
docker compose up -d websearch searxng
```

The module is available at `http://localhost:8086` and SearXNG's UI at
`http://searxng.localhost` (via Traefik).

### Extend

- **Add engines**: edit `infra/searxng/settings.yml` and specify engines in the
  `engines:` section, or set `WEBSEARCH_ENGINES` to a comma-separated list.
- **Post-process results**: add a second tool in `service.py` that calls
  `PlatformClient` to re-rank or summarise results.
- **Emit events**: add `module.emits(...)` declarations and publish via
  `EventBus` when a search completes.
