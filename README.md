# epicurus

[![CI](https://github.com/baakhoff/epicurus/actions/workflows/ci.yml/badge.svg)](https://github.com/baakhoff/epicurus/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%203.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

> A self-hosted, modular, **local-first** personal-assistant platform: an AI agent
> plus a growing fleet of sidecar modules (calendar, notes, tasks, mail, chat,
> knowledge base, storage), private and self-hosted on your own machine or server.

**Status:** 🟡 _Working toward the **1.0.0 — Foundation complete & stable** gate (a quality
bar, not a feature checklist): the agent, LLM gateway, cross-chat memory, and web shell, the
**knowledge, storage, web-search, calendar, mail, tasks, and notes** modules, and the
**messaging** module (chat bridges — loopback, Discord, Telegram) have all shipped; Slack and
WhatsApp arrive at milestone **2.0.0**. Latest tagged release: **v0.2.0**._ ·
**License:** [AGPL-3.0](LICENSE)

epicurus runs on your own machine under Docker. A **core** service runs the agent
and platform capabilities; every capability is a **sidecar module** the agent can
use. Modules talk to the core over one standardized, local-only contract.

## Documentation

Full documentation lives in **[`docs/`](docs/)** (and is published to the GitHub
Wiki once the wiki is enabled). Start here:

| If you want to… | Read |
| --- | --- |
| Install & run epicurus | [User Guide → Installation](docs/user/installation.md) |
| Configure it / manage secrets | [User Guide → Configuration](docs/user/configuration.md) |
| Understand how it's built | [Developer Guide → Architecture](docs/developer/architecture.md) |
| Build a module | [Developer Guide → Building a module](docs/developer/building-a-module.md) |
| Look up a class or function | [API Reference](docs/reference/index.md) |
| Contribute | [Contributing](docs/developer/contributing.md) |

## Quickstart

Bring up the platform's backing services (Postgres, Valkey, NATS, Qdrant, OpenBao):

```bash
git clone https://github.com/baakhoff/epicurus.git
cd epicurus
docker compose -f infra/compose/docker-compose.yml up -d   # or: task infra-up
```

…or bring up the **whole platform** (core, web UI, and the echo demo module) and
open the shell in a browser:

```bash
docker compose up -d           # or: task up
# then open http://localhost:8088/  — chat, model manager, modules, power toggle
```

Develop against the shared library and run the gates:

```bash
uv sync --all-packages
uv run pytest           # and: ruff check . · ruff format --check . · mypy
# or simply: task check
```

See the [Developer Guide](docs/developer/index.md) for the full setup.

## Contributing

Issues and pull requests welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). By
contributing you agree your contributions are licensed under the AGPL-3.0.
