# epicurus — Architecture

> Self-hosted, modular, local-first personal-assistant platform. An AI agent
> plus a growing fleet of integration modules, reachable only over Tailscale.

This document is the source of truth for *how the system is shaped*. The
delivery order lives in [ROADMAP.md](ROADMAP.md); the *why* behind each major
choice lives in [DECISIONS.md](DECISIONS.md).

## 1. Guiding principles

- **Local-first, private-by-default.** Nothing leaves the box unless a module
  explicitly needs it. Ingress is Tailscale-only.
- **Microservices from day one.** Every capability is an independently
  deployable, replaceable service behind a stable contract.
- **Two backbone contracts.** Modules talk to the core in exactly two ways:
  - **MCP (Model Context Protocol)** for synchronous *tools* the agent can call.
  - **NATS events** for asynchronous *things that happened* (e.g. a new message).
  Adding a new block = implement one MCP server (+ optional event consumer) and
  register it. This is the single decision that makes the system extensible.
- **Built public-ready.** Developed to open-source / SaaS hygiene even while the
  repo is private: zero secrets in git, clean config boundaries, documented
  contracts, ADRs. Going public/SaaS later is a switch, not a cleanup.
- **Dual-track by construction.** One codebase must fork into both an
  open-source self-hostable product and a multi-tenant SaaS (open-core). This
  imposes three binding rules from Phase 0: **(a)** `tenant_id` is a first-class,
  system-wide primitive scoping every row / event / vector collection / secret
  path / bucket — even with one tenant; **(b)** services are stateless with
  externalized state; **(c)** storage and LLM sit behind swappable backends
  (local-FS ↔ S3, Ollama ↔ hosted). SaaS-only concerns (billing, metering,
  signup) live in a separate overlay, absent from the OSS build. See
  [DUAL-TRACK.md](DUAL-TRACK.md).
- **Observable at every stage.** Structured logs + traces from every service.

## 2. Topology

```
                  Tailscale (only ingress)
                          │
                    ┌─────▼─────┐
                    │  Gateway   │  Traefik: reverse proxy + auth offload
                    └─────┬─────┘
        ┌─────────────────┼───────────────────────────┐
        │                 │                            │
   ┌────▼────┐      ┌──────▼──────┐              ┌──────▼──────┐
   │  Web UI │      │   Agent     │  MCP host    │  Identity   │ users / sub-users
   │ (PWA)   │◄────►│ orchestrator│◄──┐          │   (auth)    │
   └─────────┘      └──────┬──────┘   │ MCP       └─────────────┘
                           │          │
                    ┌──────▼──────┐   │   ┌─────────── modules (MCP servers) ──────────┐
                    │ LLM gateway │   └──►│ knowledge · storage · google · telegram ·  │
                    │ Ollama +    │       │ discord · whatsapp · slack · jira · search │
                    │ LiteLLM     │       └────────────────────────────────────────────┘
                    └─────────────┘
  ── shared infra ──  NATS · Postgres · Redis · Qdrant · MinIO
  ── platform ──      OpenBao (secrets) · Observability (Grafana/Loki/Prometheus/OTel)
                      · Restic (backup) · gluetun (per-service VPN)
```

## 3. Component map

| Concern | Choice | Role |
| --- | --- | --- |
| Local LLM runtime | **Ollama** | Runs local models on the RTX 3080; model pull/list API powers the model-management UI |
| Model routing / fallback | **LiteLLM** | One OpenAI-compatible endpoint over local + hosted models; routing, fallback, cost/log capture |
| Agent loop | **Custom thin loop** | Hand-rolled orchestrator over LiteLLM + MCP — the owned core |
| Event bus | **NATS (JetStream)** | Async pub/sub + persistence; the module event backbone |
| Vector DB | **Qdrant** | RAG + semantic memory |
| Relational DB | **Postgres** (schema-per-service) | Durable structured state |
| Cache / queues / rate-limit | **Valkey** (Redis-compatible, BSD) | Ephemeral state; BSD avoids Redis's SSPL anti-SaaS license |
| Cloud storage over HDD | **storage service** + **MinIO** | Read-index the existing HDD tree; MinIO for app-managed objects |
| Web search (free) | **SearXNG** | Self-hosted metasearch, no API keys |
| Secrets | **OpenBao** | OSS Vault fork; the one place credentials live |
| Gateway / reverse proxy | **Traefik** | Docker-label service discovery + auth middleware |
| Identity / sub-user | custom workspace model → **Authentik** later | Personal/work workspaces, scoped tokens, OAuth for the public API |
| Per-service VPN | **gluetun** sidecar | `network_mode: service:gluetun`; one instance per VPN profile |
| Observability | **structlog → Loki**, **Prometheus**, **OTel → Tempo**, **Grafana** | Logs + metrics + traces on every stage |
| Backups | **Restic** | Encrypted, restore-from-anywhere of DBs, vectors, objects, config, chats |
| Frontend | **React + Next.js** (Tailwind + shadcn/ui), PWA | Phone-friendly chat + model manager + file browser + admin |
| Ingress | **Tailscale** | Only path into the system |

## 4. The module contract

A module is a container that:

1. **Serves MCP tools.** Each tool = a typed capability the agent may call
   (`calendar.create_event`, `knowledge.search`, `storage.read_file`, …).
2. **Optionally consumes/publishes NATS events.** e.g. the Telegram module
   publishes `inbox.message.received`; the agent subscribes and may reply.
3. **Exposes standard ops endpoints** — `/health`, `/metrics` — and emits
   structured logs + OTel traces via the shared `epicurus-core` library.
4. **Fetches its own secrets from OpenBao** at runtime; nothing in env files or git.

New blocks are scaffolded from `templates/service-template/` so every module
starts with the contract wired in.

## 5. Repo shape (monorepo)

```
epicurus/
  docker-compose.yml / *.prod.yml       # orchestration
  .env.example                           # every var documented, no secrets
  Taskfile.yml                           # dev commands
  docs/                                  # ARCHITECTURE, ROADMAP, DECISIONS (ADRs)
  libs/epicurus-core/                    # shared: config, logging, NATS, MCP base, auth client, models
  services/
    gateway/ agent/ llm/ knowledge/ memory/ storage/ identity/ web/
    integrations/{google,telegram,discord,whatsapp,slack,jira,websearch}/
    messaging/                           # normalizes all chats into one inbox event schema
  infra/{observability,backup,vpn}/
  templates/service-template/            # cookiecutter — scaffolds a new module
```

Monorepo, not many repos: solo development, atomic cross-service changes, one
CI, one shared library. Each service still builds and deploys independently.

## 6. Cross-cutting concerns

- **Memory.** A dedicated memory service stores conversation summaries + facts
  in Postgres (structured) and Qdrant (semantic recall); the agent assembles
  context from it on every turn → cross-chat memory.
- **Identity & sub-user.** A workspace model (personal vs work) scopes data and
  credentials; the work "sub-user" is a separate workspace with its own secrets.
- **Public API.** The gateway exposes a versioned REST surface (FastAPI OpenAPI
  docs) secured by OAuth2 tokens, so external services can drive epicurus.
- **Secret hygiene.** `.gitignore` + gitleaks (CI + pre-commit) + OpenBao means
  credentials never touch git, by construction.
```
