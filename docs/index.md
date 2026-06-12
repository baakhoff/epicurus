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

## Documentation layers

- **[User Guide](user/index.md)** — run and configure epicurus on your own
  machine or server.
- **[Developer Guide](developer/index.md)** — the architecture and how to build
  a module, fix a bug, or contribute.
- **[API Reference](reference/index.md)** — every class and function in
  `epicurus-core`.

## What's here today

- **Web UI shell** — a phone-first PWA: chat with the agent, manage models and
  provider keys, toggle power, and configure modules.
- **Core runtime** — the agent loop, the multi-provider LLM gateway (local Ollama +
  hosted providers), cross-chat memory, power states, and the platform API.
- **`epicurus-core`** — the shared library modules are built on: configuration,
  structured logging, tenant scoping, the NATS event client, and the MCP module
  base + manifest.
- **Data plane** — the backing services (Postgres, Valkey, NATS, Qdrant, OpenBao)
  brought up with one command.

## License

epicurus is licensed under the [GNU AGPL-3.0](https://github.com/baakhoff/epicurus/blob/main/LICENSE).
