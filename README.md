# epicurus

> A self-hosted, modular, **local-first** personal-assistant platform: an AI agent
> plus a growing fleet of sidecar modules (calendar, notes, tasks, mail, chat,
> knowledge base, storage), private and self-hosted on your own machine or server.

**Status:** 🚧 _Phase 0 — building the platform skeleton._ ·
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

Develop against the shared library and run the gates:

```bash
uv sync --all-packages
uv run pytest           # and: ruff check . · ruff format --check . · mypy -p epicurus_core
# or simply: task check
```

See the [Developer Guide](docs/developer/index.md) for the full setup.

## Contributing

Issues and pull requests welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). By
contributing you agree your contributions are licensed under the AGPL-3.0.
