# Developer Guide

This guide is for anyone building a module, fixing a bug, or contributing to
epicurus.

## Start here

- **[Architecture](architecture.md)** — how epicurus is put together: the core,
  sidecar modules, and the contract between them.
- **[Development setup](development-setup.md)** — get a working dev environment.
- **[Building a module](building-a-module.md)** — add a capability with the
  `epicurus-core` library.
- **[Testing](testing.md)** — the quality gates and how tests are written.
- **[Contributing](contributing.md)** — the workflow for getting a change merged.
- **[Releases](releases.md)** — versioning and how releases are cut.

## Tech stack

- **Python 3.11+**, async throughout; FastAPI + Pydantic v2.
- **[uv](https://docs.astral.sh/uv/)** workspace (one lockfile for the monorepo).
- **MCP** (Model Context Protocol) for the agent↔module tool contract; **NATS**
  (JetStream) for events.
- Data: **Postgres**, **Valkey**, **Qdrant**; secrets in **OpenBao**.
- Tooling: **ruff**, **mypy** (`--strict`), **pytest**.

## Repository layout

```
epicurus/
  libs/epicurus-core/   # shared library every service builds on
  services/             # deployable services / modules (one dir each)
  infra/                # compose stack and operational config
  templates/            # scaffolding for new modules
```
