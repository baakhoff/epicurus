# Changelog

All notable changes to epicurus are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`v0.1.0` is the first release — the first version usable on a server with a UI.

A release is cut by pushing a semver tag (`git tag v0.1.0 && git push origin
v0.1.0`); GitHub Actions then publishes the GitHub Release and versioned container
images to GHCR.

## [Unreleased]

Phase 2 (knowledge & storage) is in progress.

### Added

- **Shared chat contract** — `ChatMessage` and `ChatResult` are now exported from
  `epicurus_core` as the single source of truth for the chat shapes the gateway,
  platform API, and `PlatformClient` all use (#114).
- **LLM tuning via env** — `LLM_TEMPERATURE`, `LLM_TOP_P`, and `LLM_NUM_CTX`
  (alongside the existing `LLM_KEEP_ALIVE`) flow through compose → settings →
  gateway, so tuning needs no code edit (#114).

### Changed

- **One module-facing chat path.** `POST /platform/v1/chat` is now the single
  module → core chat endpoint and returns the shared `ChatResult`. `PlatformMessage`
  and `PlatformChatResponse` are kept as backward-compatible aliases of
  `ChatMessage` / `ChatResult` (#114). `epicurus-core` and `core-app` → **0.2.0**.

### Removed

- **`POST /platform/v1/llm/chat`** — folded into `POST /platform/v1/chat`, a strict
  superset (it also accepts `tools` and `tenant_id`). `PlatformClient` already used
  `/chat`, so live module code is unaffected (#114).

### Fixed

- **Smoke gate isolation** — `infra/ci/compose.ci.yaml` now resets host ports for the
  wave-2 modules (calendar, mail, tasks) too, so `task smoke` runs alongside a
  developer's dev stack without port collisions, as its header promises (#114).

## [0.1.0] — 2026-06-12

**Phase 1 — the core runtime.** The platform runs end to end: chat from a phone with
a local or hosted model that calls tools and remembers across sessions.

### Added

- **Agent** — a thin MCP tool-calling loop with streaming chat (SSE).
- **LLM gateway** — one provider-agnostic interface over local **Ollama** and hosted
  providers (Claude, ChatGPT, Grok, DeepSeek, Gemini, and any OpenAI-compatible
  endpoint): routing, fallback chains, and tenant-scoped usage accounting. Keys live
  in OpenBao, never in env or logs.
- **Power states** (Active / Idle / Paused) with idle model unload (ADR-0005).
- **Cross-chat memory** — conversation history in Postgres plus semantic recall over
  Qdrant embeddings, scoped per tenant.
- **Web UI shell** — a phone-first PWA (chat, model manager, provider keys, power
  toggle) that renders each module's UI declaratively from its manifest (ADR-0007).
- **Module manifest UI** — `UiSection` / `UiAction`, served at `GET /manifest`.

### Known limitations

An early `0.x` release for personal / self-host use:

- **Secrets are not yet persistent** — OpenBao runs in dev (in-memory) mode, so
  provider keys and module config are lost when the `openbao` container restarts.
  Persistent secret storage lands in Phase 3.
- **The event bus has no authentication** — NATS tenant isolation is cooperative
  (fine single-user, not multi-tenant). Tracked in #50.
- **No perimeter is bundled** — the edge gateway only routes; put your own access
  layer (VPN / reverse proxy / auth proxy) in front (ADR-0008).
