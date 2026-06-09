# epicurus

> A self-hosted, modular personal-assistant platform. Local-first AI agent +
> a growing fleet of integration modules (calendar, notes, tasks, mail, chat,
> knowledge base, cloud storage), accessed privately over Tailscale.

**Status:** 🌱 _Bootstrapping._ This is a void repo — the architecture is being
planned before any service code lands. See the plan in the issue tracker /
`docs/` once approved.

## Vision

A private, extensible "second brain + operator" that runs on a home Windows
machine under Docker, reachable only over Tailscale. It pairs a local-first AI
agent (with optional hosted-API fallback) with pluggable modules so new
capabilities can be added as self-contained services.

### North-star capabilities

- **AI agent** with tool/function calling; **local models prioritized**, hosted
  API optional.
- **RAG** over an Obsidian knowledge base.
- **Google** Calendar / Tasks / Notes / Mail integration.
- **Cloud storage** layered over an existing HDD of files, browsable by the agent.
- **Chat bridges**: Telegram, WhatsApp, Discord (read + reply from the app).
- **Work tools**: Jira, Slack personal-profile integration.
- **Cross-chat long-term memory.**
- **Web search** for the agent via free providers.
- **Per-service VPN routing** profiles.
- **Work "sub-user"** isolation.
- **Extensive public API** for connecting external services.
- **Strong secret storage** for the many personal credentials involved.
- **Backups** of everything (chats, data, config) — restorable from anywhere.
- **Logging & debugging** at every stage.
- **Model management UI**: download/switch models, with quality guidance.
- **Phone-friendly web UI.**

## Principles

- **Local-first, private-by-default.** No data leaves the box unless a module
  explicitly needs it.
- **Microservices from day one** — each block is an independently deployable,
  replaceable service behind a stable contract.
- **Prepared to be public.** Developed to open-source / SaaS hygiene even while
  the repo is private: zero secrets in git, clean config boundaries, documented
  contracts.
- **Scalable, sustainable, boring-where-it-counts.** Build the core to last.

## License

Not yet chosen — see the planning discussion. Until a license is added, all
rights reserved.
