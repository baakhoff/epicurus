# Changelog

All notable changes to epicurus are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases begin at **v0.1.0** — the first version usable on a server with a UI.
Until then the platform is being built and there is nothing to release.

A release is cut by pushing a semver tag (`git tag v0.1.0 && git push origin
v0.1.0`); GitHub Actions publishes a Release with notes grouped by `type:*` label.

## [Unreleased]

**Phase 1 (the core runtime) is complete** — the platform now runs end to end,
though it stays pre-release until the first tagged version.

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

Phase 2 (knowledge & storage) is next.
