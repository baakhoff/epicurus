# Architecture Decision Records

Short, dated records of the choices that shape epicurus and *why*. Newest at the
bottom. When a decision is reversed, add a new ADR that supersedes the old one
rather than editing history.

---

## ADR-0001 — Core stack & architecture — 2026-06-09 — Accepted

**Context.** Greenfield self-hosted personal-assistant platform on a home
Windows machine (Docker, RTX 3080), accessed only over Tailscale. Must be
local-first, extensible with new "blocks", and developed to public/SaaS-ready
standards while staying private for now.

**Decisions.**

- **Microservices from day one**, monorepo. Solo dev wants atomic cross-service
  changes and one CI; services still deploy independently.
- **Backbone contracts: MCP + NATS.** Modules expose synchronous tools via MCP
  (the agent is the MCP host) and async events via NATS. Adding a block = one
  MCP server. MCP doubles as the external/agent API surface.
- **Backend: Python / FastAPI** across services.
- **Agent: custom thin loop** over LiteLLM + MCP. Chosen over Pydantic-AI /
  LangGraph: the user explicitly wants to own the core from the ground up; a
  hand-rolled loop maximizes control and understanding. Trade-off: we maintain
  the agent logic ourselves.
- **LLM: Ollama (local, RTX 3080) + LiteLLM** as an OpenAI-compatible routing
  proxy with hosted-API fallback. Ollama's model API powers the model-manager UI.
- **Secrets: OpenBao** (OSS Vault fork). Chosen over Infisical (friendlier UI)
  and SOPS (lightest) for a serious, fully-OSS secrets engine that fits the
  public-ready goal. Trade-off: heavier ops, less casual UI.
- **Frontend: React + Next.js** (Tailwind + shadcn/ui), PWA. The user is not a
  frontend developer and wants the stack that is *easiest to maintain with AI
  assistance*. React/Next.js has the largest model-training presence (most
  reliable AI-generated changes) and shadcn/ui minimizes hand-written UI.
  Chosen over SvelteKit (leaner but less AI-represented) and HTMX (fewer
  languages but weaker mobile chat/PWA UX).
- **Data:** Postgres (schema-per-service), Redis (cache/queues), Qdrant
  (vectors), MinIO (app-managed objects over the HDD).
- **Edge & ops:** Traefik gateway, Tailscale-only ingress, gluetun for
  per-service VPN, Restic for backups, Grafana/Loki/Prometheus/Tempo + OTel for
  observability, SearXNG for free web search.

**Consequences.** The core (Phase 0) must land the shared `epicurus-core`
library, the service template, and the MCP+NATS contract before feature modules.
GPU reaches Docker via WSL2. Public/SaaS migration stays a switch, not a rewrite.

**Open / deferred.**

- **License** not yet chosen (currently all-rights-reserved). Candidates: AGPL
  (protects a future SaaS) vs permissive (Apache-2.0). Decide before public.
- **Identity** starts as a custom workspace/JWT model; may adopt Authentik
  (full IdP) when sub-user/OAuth needs grow.
- **3080 VRAM** (10GB assumed) caps comfortable local models around 14B
  quantized; larger models offload to CPU or use hosted fallback.

---

## ADR-0002 — Dual-track: open-source + SaaS from one codebase — 2026-06-09 — Accepted

**Context.** epicurus must, from the beginning, be able to fork into two
products: a public open-source self-hostable platform, and a multi-tenant
hosted SaaS. These have partly opposite requirements (single-user/private/local
vs multi-tenant/public/hosted). Preparing structurally up front avoids an
expensive and data-leak-prone retrofit later.

**Decision.** Adopt an **open-core** model with binding rules enforced from
Phase 0. Full strategy in [DUAL-TRACK.md](DUAL-TRACK.md). Key points:

- **Tenant is a first-class, system-wide primitive from Phase 0.** `tenant_id`
  scopes every row, NATS subject, Qdrant collection, OpenBao secret path, and
  object bucket — even with one tenant. No single-global-tenant code paths.
- **Stateless services, externalized state.** Enables both "one box" and
  "N replicas" from the same code.
- **Backend-swappable storage & LLM.** local-FS ↔ S3, Ollama ↔ hosted.
- **SaaS-only concerns (billing, metering, signup, quotas) live in a private
  overlay** absent from the OSS build; core emits usage events, overlay consumes.
- **Valkey instead of Redis** to avoid Redis's SSPL anti-SaaS license.
- **AGPL components (Grafana/Loki/Tempo, SearXNG, MinIO) deployed unmodified
  only** — never forked into our codebase.

**Consequences.** `epicurus-core` and the service template must ship the tenant
context and statelessness conventions in Phase 0. The architecture's
microservices + externalized state + LiteLLM abstraction already align with
this, so the cost is mostly discipline, not redesign.

**Open / deferred.**

- **Our code's license** — AGPL-3.0 + CLA (recommended, preserves dual-license
  right and deters competitor SaaS) vs Apache-2.0 (adoption-friendly, no moat).
  Must be decided **before** the repo goes public.
- **Public auth/IdP** for SaaS (Authentik or hosted provider) — designed in
  Phase 5, hardened before SaaS launch.

---

## ADR-0003 — Engineering process & tooling — 2026-06-09 — Accepted

**Context.** Up to four agents (plus the owner) work this repo in parallel, all
pushing as the same GitHub account. We need a workflow that keeps `main` clean,
prevents agents clashing, and stays reproducible — while remaining public-ready.

**Decision.**

- **Workflow:** every unit of work goes worktree → branch → build → thorough
  tests → thorough docs → commit → PR, then waits for the owner to review, merge,
  and rebuild. Detailed in [AGENTS.md](AGENTS.md).
- **`main` protection:** PR-only, no direct/force pushes. Server-side rulesets
  need GitHub Pro (or a public repo), so for now this is enforced by a local
  `pre-push` hook in `.git/hooks` (covers every worktree on this machine); owner
  override is `git push --no-verify`. Revisit on upgrade or open-sourcing.
- **Parallel coordination:** a GitHub **Projects board** (Todo → In Progress →
  In Review → Done). Since all agents share one GitHub identity, the claim signal
  is board state + a **Branch** field on the card, not the assignee.
- **Dependency & env tooling: uv** (workspace + single lockfile). Chosen for a
  multi-service monorepo maintained largely with AI assistance: one fast tool,
  uniform commands, one `uv.lock` keeping every package coherent, and fast
  edit→lock→test loops. Preferred over Poetry (per-project, slower) and pip.
- **Conventional Commits**, `.editorconfig`, and a `pre-commit` config (ruff +
  gitleaks + hygiene hooks) mirrored by CI (ruff, mypy --strict, pytest, gitleaks).
- **No AI/assistant attribution anywhere in the repo** (commits, PRs, comments,
  docs, branch names). History was squashed to a clean root to honor this.

**Consequences.** New work is uniform and low-conflict; CI gates quality on every
PR. The local-hook protection is weaker than server-side rules — accepted
trade-off until Pro/public.

**Open / deferred.**

- Server-side branch protection (needs Pro or public).
- Automated changelog / release tagging from Conventional Commits (later).
- Community-health files (CONTRIBUTING, SECURITY, issue/PR templates) before public.

---

## ADR-0004 — Module model: core + sidecars, bidirectional local-only contract — 2026-06-09 — Accepted

**Context.** The product is a core container (epicurus = agent + platform) with
each capability added as a sidecar container that acts as a tool/function for the
agent. We need a connection model that lets any container call anything epicurus
provides, stays safe, and supports a future community ecosystem.

**Decision.**

- **Core + sidecar modules.** epicurus runs as the core container; capabilities
  are sidecar containers. Adding a block = running one more container that speaks
  the contract.
- **One standardized, bidirectional contract:** MCP (module → agent tools), a
  core **platform API** (module → core: secrets, events, storage, agent/LLM, tool
  registry), and NATS events (either direction). Its shape stays stable as it
  improves.
- **Local-only trust boundary.** The contract is private to the internal Docker
  network, not exposed externally by default. External access is a deliberate,
  gated **business-tier** capability later (distinct from Tailscale user ingress).
- **Module manifest.** Each module ships a manifest (image, tools, events,
  config/secrets, contract version) — the basis for both the service template and
  the future installer.
- **Ecosystem (Phase 7).** A one-click "add by domain" installer reads a module's
  manifest from a URL, starts the container, and registers its tools. The
  community **marketplace website is a separate project**; this repo provides only
  the manifest spec + installer.
- **Native apps (future).** Windows / Linux / macOS desktop apps that package the
  full platform to run entirely locally — a separate track, recorded in the roadmap.

**Consequences.** The Phase 0 MCP base + core platform API must be designed
bidirectional and local-only from the start. The manifest becomes a first-class
artifact. External exposure aligns with the SaaS / business overlay (ADR-0002,
DUAL-TRACK), not the OSS default.

**Open / deferred.**

- Exact platform-API surface and transport (REST/gRPC over the Docker network) —
  designed alongside the MCP base classes.
- Manifest schema and contract-versioning scheme — drafted before Phase 7.
