# Developer Guide

This guide is for anyone building a module, fixing a bug, or contributing to
epicurus.

## Start here

- **[Architecture](architecture.md)** — how epicurus is put together: the core,
  sidecar modules, and the contract between them.
- **[Development setup](development-setup.md)** — get a working dev environment.
- **[Building a module](building-a-module.md)** — add a capability with the
  `epicurus-core` library.
- **[API Reference](../reference/index.md)** — every class and function in
  `epicurus-core`, and how they fit together.
- **[Testing](testing.md)** — the quality gates and how tests are written.
- **[Contributing](contributing.md)** — the workflow for getting a change merged.
- **[Versioning](versioning.md)** — per-component SemVer and the bundled-stack tag.
- **[Releases](releases.md)** — how a release is cut and published.

## Research & spikes

- **[Obsidian Sync spike](obsidian-sync-spike.md)** — feasibility of syncing the
  knowledge vault with the user's Obsidian Sync vault, and the recommended path (#219).

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
