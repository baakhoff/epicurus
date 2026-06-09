# epicurus

**epicurus** is a self-hosted, modular, **local-first** personal-assistant
platform. A core service runs an AI agent and a set of platform capabilities;
every additional capability is a **sidecar module** that the agent can use.

It is designed to run on your own machine or server under Docker and to stay
private — you choose how to reach it (locally, over your LAN, behind a VPN, or
however you expose your own server).

> **Early development.** epicurus is being built. This documentation covers what
> exists today and grows as capabilities land. There is not yet an end-user app —
> the current surface is the platform itself (the data-plane services and the
> `epicurus-core` library that modules are built on).

## Documentation layers

- **[User Guide](user/index.md)** — run and configure epicurus on your own
  machine or server.
- **[Developer Guide](developer/index.md)** — the architecture and how to build
  a module, fix a bug, or contribute.
- **[API Reference](reference/index.md)** — every class and function in
  `epicurus-core`.

## What's here today

- **`epicurus-core`** — the shared library modules are built on: configuration,
  structured logging, tenant scoping, the NATS event client, and the MCP module
  base + manifest.
- **Data plane** — the backing services (Postgres, Valkey, NATS, Qdrant,
  OpenBao) brought up with one command.

## License

epicurus is licensed under the [GNU AGPL-3.0](https://github.com/baakhoff/epicurus/blob/main/LICENSE).
