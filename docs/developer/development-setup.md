# Development setup

## Prerequisites

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** — manages the virtualenv and the workspace.
- **Docker** — for the data plane and for integration tests (testcontainers).
- Optionally **[go-task](https://taskfile.dev)** for the `task` shortcuts.

## Install

```bash
git clone https://github.com/baakhoff/epicurus.git
cd epicurus
uv sync --all-packages          # installs every workspace package + dev tools
```

## Quality gates

The same gates CI runs:

```bash
uv run ruff check .             # lint
uv run ruff format --check .    # formatting
uv run mypy -p epicurus_core    # types (strict)
uv run pytest                   # tests
```

Or all at once with go-task:

```bash
task check
```

!!! tip "pre-commit"
    Install the git hooks so the gates run before each commit:
    `uv run pre-commit install`.

## Run the data plane

Most integration work needs the backing services running:

```bash
task infra-up        # or: docker compose -f infra/compose/docker-compose.yml up -d
```

See [Installation](../user/installation.md) for details and health checks.

## Repository layout

```
epicurus/
  libs/epicurus-core/   # shared library (config, logging, tenancy, events, module, manifest)
  services/             # deployable services / modules
  infra/compose/        # the data-plane compose stack
  templates/            # scaffolding for new modules
  Taskfile.yml          # dev command shortcuts
```

It is a single **uv workspace**: one `uv.lock` pins dependencies for every
package. Shared tool configuration (ruff, mypy, pytest) lives in the root
`pyproject.toml`.
