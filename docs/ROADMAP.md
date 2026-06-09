# epicurus — Roadmap

Phased delivery. The architecture is microservices from day one
([ARCHITECTURE.md](ARCHITECTURE.md)), but we ship in phases so each step is
independently useful instead of a big-bang build. Every phase ends in something
you can actually use.

Status legend: ⬜ not started · 🟡 in progress · ✅ done

**Build phasing.** Phase 0 (the foundation) is built by a single agent and
self-merged, PR by PR, until it is solid. Parallel agents are onboarded at the
start of Phase 1, when work shifts to services/modules. Live status is on the
project board.

---

## Phase 0 — Platform skeleton (the foundation) 🟡

The scalable, sustainable core — built right before anything hangs off it.

- Monorepo layout + `Taskfile` dev commands.
- `libs/epicurus-core`: config, structlog logging, NATS client, MCP base
  classes, OpenBao client, common Pydantic models, `/health` + `/metrics`.
- **Tenant primitive (dual-track foundation):** `tenant_id` threaded through
  `epicurus-core` — DB scoping, NATS subjects, Qdrant collections, OpenBao
  secret paths, object buckets — even though v1 runs a single tenant. Plus the
  binding design rules: stateless services, externalized state, and
  backend-swappable storage/LLM. See [DUAL-TRACK.md](DUAL-TRACK.md).
- `docker-compose.yml` with: **Traefik**, **Postgres**, **Valkey**, **NATS**,
  **Qdrant**, **OpenBao**, and the observability stack
  (**Grafana / Loki / Prometheus / Tempo / OTel collector**).
- `templates/service-template/` (cookiecutter) — scaffolds a module with the
  MCP + events + ops contract already wired.
- One trivial **"echo" module** proving the MCP tool-call + NATS event path
  end-to-end.
- **Tailscale** ingress.
- CI: ruff + mypy (`--strict`) + pytest; **gitleaks** secret-scanning;
  pre-commit hooks.

**Done when:** the stack comes up with `docker compose up`, the echo module's
tool is callable through the agent skeleton, and logs/traces show up in Grafana.

## Phase 1 — Agent MVP ⬜

- **LLM service**: Ollama + LiteLLM; model pull/list API.
- **Agent orchestrator**: custom thin loop with MCP tool-calling + streaming
  chat API.
- **Memory v1**: store + semantic recall across chats.
- **Web UI**: minimal phone-friendly React/Next.js PWA chat shell.

**Done when:** you can chat with a local model that calls tools, remembers
across sessions, from your phone over Tailscale.

## Phase 2 — Knowledge & storage ⬜

- **Knowledge service**: Obsidian vault RAG — incremental index → Qdrant; agent
  retrieval tool.
- **Storage service**: index the existing HDD tree; browse / search / download
  API; agent file-access tool; MinIO for app-managed objects.

## Phase 3 — Google, web search & secrets ⬜

- **Google** Calendar / Tasks / Mail modules (OAuth).
- **SearXNG** web-search tool (free, self-hosted).
- **OpenBao** wired in as the credential source for all modules.

## Phase 4 — Chat bridges ⬜

- **Telegram** + **Discord** first (easiest), then **Slack**, then **WhatsApp**
  (hardest).
- **messaging** service: normalize all chats into one inbox event schema so the
  agent can read and reply everywhere.

## Phase 5 — Work profile & networking ⬜

- Multi-workspace **identity** (personal vs work).
- **Jira** + **Slack** work-profile integration.
- **gluetun** VPN-routing profiles (route chosen services through a VPN).
- Work **sub-user** isolation.

## Phase 6 — Hardening & ops ⬜

- **Restic** backups (encrypted, restore-from-anywhere): DBs, vectors, objects,
  config, chats.
- **Model-management UI** polish (download/switch models, quality guidance).
- **Public OAuth API** for external services.
- Full **PWA** (installable, offline shell).
- Docs + ADRs complete.

## Phase 7 — Ecosystem & extensibility ⬜

Make epicurus a platform others build on.

- **Module manifest spec** — the standardized descriptor a module ships (image,
  tools, events, required config/secrets, contract version).
- **One-click installer ("add by domain")** — OpenWebUI-style: paste a module's
  URL/domain → epicurus fetches the manifest, starts the container, wires it onto
  the local contract, and registers its tools with the agent. Press add, it appears.
- **Module SDK & docs** so open-source contributors can build their own modules.

> The community **marketplace website** (browse/share modules) is a **separate
> project**, out of scope for this repo. This phase ships only what the core
> needs: the manifest spec and the installer that consumes it.

---

## Future directions (beyond the phased build)

- **Native desktop apps — Windows, Linux, macOS.** Package the full platform to
  run entirely locally on a machine, keeping the same modular, local-first
  principles (agent, modules, and contract — no cloud required). A large,
  separate track; recorded here so it is not forgotten.

---

## Requirement → where it lands

| Your requirement | Phase | Component |
| --- | --- | --- |
| AI agent + optional API (local-first) | 1 | agent + LLM (Ollama/LiteLLM) |
| RAG over Obsidian | 2 | knowledge |
| Google calendar / notes / tasks / mail | 3 | integrations/google |
| Cloud storage over existing HDD | 2 | storage + MinIO |
| Phone UI | 1, 6 | web (PWA) |
| Agent web search (free) | 3 | integrations/websearch (SearXNG) |
| Cross-chat memory | 1 | memory |
| Easy adding of new blocks | 0 | service-template + MCP/NATS contract |
| Backup from anywhere | 6 | infra/backup (Restic) |
| Logging & debugging each stage | 0 | observability stack |
| Telegram / WhatsApp / Discord | 4 | integrations + messaging |
| Jira / Slack work profile | 5 | integrations + identity |
| Per-service VPN routing | 5 | infra/vpn (gluetun) |
| Work sub-user | 5 | identity |
| Extensive external API | 0, 6 | gateway + MCP contract |
| Strong secret storage | 0, 3 | OpenBao |
| Agent access to container file storage | 2 | storage |
| Download/change models via UI | 1, 6 | LLM + web |
| Standardized module contract (bidirectional, local-only) | 0 | epicurus-core (MCP + platform API + NATS) |
| Community modules + one-click "add by domain" | 7 | manifest spec + installer |
| Native Windows / Linux / macOS apps | Future | desktop packaging |
