# Architecture

epicurus is a **core service** surrounded by **sidecar modules**. The core runs
the agent and the platform capabilities; each module is a container that adds a
capability the agent can use.

## Core + sidecar modules

- The **core** *is* epicurus: the agent plus platform services.
- Every capability (calendar, knowledge, a chat bridge, …) is a **sidecar
  container** running alongside the core. Adding a capability means running one
  more container that speaks the contract.

## The contract

Modules and the core talk over one standardized, **bidirectional** contract:

- **MCP tools (module → agent).** A module exposes typed tools the agent can
  call (e.g. `calendar.create_event`). This uses the
  [Model Context Protocol](https://modelcontextprotocol.io).
- **Platform API (module → core).** A module can call back into the core for the
  capabilities it provides — events, secrets, storage, and **AI inference**.
- **Events (either direction).** Asynchronous "something happened" messages over
  NATS (e.g. a module publishes `inbox.message.received`; the agent reacts).

> **Local-only.** The module↔core contract runs over the internal Docker network
> only. It is not exposed externally by default.

## All AI goes through the core

Modules never call language models directly or hold model API keys. They request
inference from the core, which owns model selection and routing (local models via
[Ollama](https://ollama.com), or hosted models, behind a single gateway), plus
the keys and usage logging. This keeps model credentials in one place and lets a
module work the same whether the model is local or hosted.

## The manifest

Each module ships a **manifest** describing itself: its identity, the tools it
serves, the events it emits and consumes, and the config/secrets it needs. The
manifest is generated from the module's registered tools and declared events (see
[Building a module](building-a-module.md)).

## Tenant scoping

Every addressable resource is **namespaced by tenant** — NATS subjects, Qdrant
collections, OpenBao secret paths, and object-storage buckets all carry a tenant
prefix. A single-tenant self-host install uses one tenant (`local` by default);
the same code keeps tenants isolated when there is more than one.

## Core services

Alongside the data plane, three application services run in the stack:

| Service | Role |
| --- | --- |
| **core-app** | the brain — the agent loop, the LLM gateway, cross-chat memory, power states, the MCP host, and the platform API ([details](../../services/core-app/README.md)). |
| **web** | the web UI shell — a phone-first PWA: chat, model manager, power toggle, and manifest-driven module UI ([details](../../services/web/README.md)). |
| **echo** | the reference module — proves the MCP tool + NATS event contract end to end. |

`core-app` and `web` are the foundational core (ADR-0009); `echo` is the template
every new module follows.

## Data plane

The backing services every module can rely on:

| Service | Role |
| --- | --- |
| **Postgres** | relational store (schema-per-service) |
| **Valkey** | cache, queues, rate-limiting (Redis-compatible) |
| **NATS** (JetStream) | the event backbone |
| **Qdrant** | vector database |
| **OpenBao** | secrets |
| **MinIO** | S3-compatible object store for app-managed objects (buckets are tenant-scoped; the storage module writes here) |

These come up with the [compose stack](../user/installation.md).

## `epicurus-core`

The shared library every service imports. It provides the building blocks the
contract is made of:

- **config** — environment-driven settings.
- **logging** — structured logging (structlog).
- **tenancy** — tenant validation and the scoping helpers above.
- **events** — the async NATS client (`EventBus`).
- **module / manifest** — the MCP module base (`EpicurusModule`) and the manifest
  model (including the declarative module UI, ADR-0007).
- **secrets** — the OpenBao-backed secret store (`SecretStore`).
- **observability** — the shared `/health` and `/metrics` endpoints.
