# Architecture

epicurus is a **core service** surrounded by **sidecar modules**. The core runs
the agent and the platform capabilities; each module is a container that adds a
capability the agent can use. That is the system today: one server, one web UI
(also wrapped by a desktop app), one tenant, plus an opt-in agent host that nothing
drives yet. The [three parts](#the-three-parts) section below describes where the
project is heading and marks, sentence by sentence, what is built and what is not.

## The three parts

> **Read this first.** Today epicurus is **one server, one web UI and one tenant**, plus two
> first pieces that stand on their own: a **desktop app** that is a window onto that server's
> web UI, and **Hermarchus v0**, an opt-in agent host that nothing in the stack drives yet.
> Everything below marked **Planned** is a direction, not a feature: there are no profiles, no
> user accounts beyond sign-in, no local or remote projects, no client core, no network paths,
> and no link between the server and an agent host, and none of them can be used yet. The plan
> is tracked in epic [#1014](https://github.com/baakhoff/epicurus/issues/1014).

epicurus is planned to split into three parts that can run on different machines.

### Epicurus, the client

- **Today.** Two shells around **one UI**, the `web` app (a phone-first PWA) served by the
  server. In a browser it is the [web shell](../services/web.md). The
  [desktop app](../apps/desktop.md) (`apps/desktop`, #1015) is an Electron window for macOS,
  Windows and Linux that connects to **one** server, loads that server's web UI and remembers
  the server and the sign-in; it bundles no UI and runs nothing locally, so it does nothing
  without a server. Its builds come from CI, not signed with a certificate, with no automatic
  update ([installing it](../user/desktop-app.md)). Neither shell keeps projects or state of
  its own beyond the browser session.
- **Planned.** A small client core behind the UI, so the desktop app works alone for local
  projects (chat, local or API models, files and terminal on that machine), connects to any
  number of Pamphilus servers, can drive a Hermarchus directly, and delegates everything else.
  It may run on a laptop or desktop; the web front runs in any browser.

### Pamphilus, the server

- **Today.** The server **is** what this page describes: the core (`core-app`), the
  model gateway, the secret store (OpenBao), the data plane and the sidecar modules,
  behind the edge gateway and the `web` shell. It is not yet called Pamphilus in the
  code or in the other docs, which say "core". It serves **one tenant** (`local` by
  default). Optional OIDC sign-in exists (`AUTH_MODE=oidc`); it admits people to that
  one tenant and does not give each of them a space of their own. The core now names the
  tenant on every module tool call and the module binds it
  ([#917](https://github.com/baakhoff/epicurus/issues/917),
  [the tenant on a tool call](../reference/modules.md#the-tenant-on-a-tool-call-917)) — the
  groundwork profiles need, not profiles.
- **Planned.** The same server gains users, profiles, remote projects, sync, a
  per-profile tool endpoint (so every agent kind can use the modules), network paths,
  an admin UI for user management, and the ability to start and supervise Hermarchus
  instances. It runs on a home server, a VPS or a cluster. The word *Pamphilus*
  previously named a planned thin inference box (#109); that is now just one model
  backend behind the gateway.

### Hermarchus, the agent host

- **Today.** [Hermarchus v0](../services/hermarchus.md) (`services/hermarchus`, #1016) is
  an **opt-in** service — a Compose overlay and a chart block, both off by default — that
  runs agent sessions over the Agent Client Protocol on one workspace filesystem. It is
  bound to one tenant id, and its controller is whoever holds its bearer token (curl, the
  tests, the smoke gates): **no part of the stack drives it**, and it talks to no part of
  the stack — no core, no gateway, no modules, so an agent in it has none of the platform's
  tools. Its only tested agent is the built-in `echo` reference agent; the entries for
  vendor programs are configured from their documentation and **not verified by running**.
  The chat agent still runs inside `core-app`.
- **Planned.** One sandbox bound to exactly **one profile**, started and supervised by a
  Pamphilus or driven by a client directly, given its tools, limits and network path by
  that controller and owning nothing of its own. It may run on the same machine as the
  server or on another one.

## Terms

| Term | Meaning | Today |
| --- | --- | --- |
| **User** | The person who signs in. Sign-in, devices, appearance, the list of profiles and the known servers live here. | **Planned.** Optional OIDC sign-in exists, but there is no per-user record, profile list or device list. |
| **Profile** | A walled space a user creates, as many as they want: sign-ins, keys, tools, rules, prompts, limits, network path, chats, files, memory. It is built on `tenant_id` and is what the roadmap called a *workspace*. | **Planned.** Today there is a single tenant and a user cannot create another. The `tenant_id` scoping it will rely on does exist (see [Tenant scoping](#tenant-scoping)); a Hermarchus v0 is bound to a tenant id. |
| **Project** | Says where things run: which profile, which Hermarchus (or none), which network path. *Local* (lives in one Epicurus client) or *remote* (lives in a Pamphilus, visible from every connected client). | **Planned.** No project concept exists. |
| **Chat** | A conversation; it belongs to a project. | **Today** chats exist, but they belong to the one tenant, not to a project. |
| **Session** | One running agent behind a chat. | **Today** only inside a Hermarchus v0, which runs sessions for whoever holds its token; no chat is attached to one. A chat turn runs the built-in agent in `core-app`. |
| **Controller** | Whatever drives a Hermarchus over its HTTP contract. | **Today** anything holding the instance's token. **Planned:** a Pamphilus or an Epicurus client. |

## How the parts connect

This is the **planned** design; apart from the first item, none of these links exist yet.

- **Three versioned contracts:** UI to client core, client core to Pamphilus, and
  controller to Hermarchus. Only the last exists today, as
  [Hermarchus contract version 1](../reference/hermarchus.md), reached with a bearer token.
- **Links are dialled outwards by the less reachable side**, enrolled with a
  one-time code, so a laptop or a home box behind NAT needs no inbound port.
- **Credentials.** API keys stay on the server. A program in a Hermarchus is to be pointed
  at the model gateway with a short-lived key and never hold an API key. A
  subscription sign-in is done once inside a Hermarchus and stays inside that one — which
  is already how Hermarchus v0 works: an operator installs and signs in to a program inside
  the instance, which holds no gateway key and sits on a network of its own, off the stack's.
- **Network path.** A profile's network path is to cover everything its sandbox
  sends (a tunnel beside the sandbox, default-deny when the tunnel is down).
- **Constraints amended on purpose** (ADR-0147). The links between a client core and a
  server, and between a controller and a Hermarchus, cross machines by design; the
  module↔core contract stays local-only (see [The contract](#the-contract)). A subscription
  agent program inside a Hermarchus calls its vendor itself rather than through the gateway;
  modules still never do (see [All AI goes through the core](#all-ai-goes-through-the-core)).
  A Hermarchus is a stateful sandbox; every other service stays stateless.

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
prefix. A single-tenant self-host install uses one tenant (`local` by default).
The core names the tenant on every module tool call (#917), but most modules still
keep their own rows under the deployment default, so a second tenant is not yet
isolated end to end — that is part of the profile work.

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
