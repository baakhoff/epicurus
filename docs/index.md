# epicurus

**epicurus** is a self-hosted, modular, **local-first** personal-assistant
platform. A core service runs an AI agent and a set of platform capabilities;
every additional capability is a **sidecar module** that the agent can use.

It is designed to run on your own machine or server under Docker and to stay
private — you choose how to reach it (locally, over your LAN, behind a VPN, or
however you expose your own server).

> **Status.** Phase 1 is complete: the agent, LLM gateway, cross-chat memory, and
> the web UI shell are live. This documentation covers what exists today and grows
> as capabilities land. Phase 2 (knowledge & storage) is next.

## Navigate

- **[User Guide](user/index.md)** — install, configure, and run epicurus.
- **[Services & Modules](services/index.md)** — a page per running block: the core
  runtime, the web shell, and each module — what it does, its tools/endpoints, config,
  and data.
- **[API Reference](reference/index.md)** — the `epicurus-core` library and the
  module↔core contracts (platform API, MCP, NATS, manifest).
- **[Infrastructure](../infra/README.md)** — the data plane (Postgres, Valkey, NATS,
  Qdrant, OpenBao, MinIO), the edge gateway, observability, and Ollama.
- **[Developer Guide](developer/index.md)** — architecture, building a module, testing,
  contributing, releases.

## The platform at a glance

```text
epicurus
├─ core
│  ├─ core-app    the brain — agent · LLM gateway · memory · power · platform API · MCP host · log stream
│  └─ web         the phone-first PWA shell — chat · models · modules · settings · observability
├─ modules        (sidecars; add one by running one more container)
│  ├─ storage     MinIO object store + chat-upload sink; agent file tools over the core file space
│  ├─ knowledge   Obsidian-vault RAG (incremental index + search)
│  ├─ calendar    provider-neutral calendar (local + Google)
│  ├─ mail        Gmail-backed mail — full client page (browse · read · reply · compose)
│  ├─ tasks       task management (Google Tasks + local store)
│  ├─ messaging   chat bridges (external channels drive a turn; reply routes back)
│  └─ echo        the contract-proof reference module
├─ data plane     Postgres · Valkey · NATS · Qdrant · OpenBao · MinIO
└─ edge & ops     Traefik gateway · Grafana / Loki / Prometheus / Tempo · Ollama
```

Every module speaks one **local-only** contract to the core — MCP tools to the agent, the
platform API to the core, NATS events either way (ADR-0004). The deep dive is in the
[Architecture](developer/architecture.md) guide.

## License

epicurus is licensed under the [GNU AGPL-3.0](https://github.com/baakhoff/epicurus/blob/main/LICENSE).
