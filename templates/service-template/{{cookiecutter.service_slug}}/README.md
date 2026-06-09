# {{ cookiecutter.service_name }}

{{ cookiecutter.description }}

An epicurus module built on `epicurus-core`: it exposes MCP tools to the agent and
can publish/consume NATS events. Start from the sample `ping` tool in
`src/{{ cookiecutter.package_name }}/service.py` and replace it with the real
capability.

## Develop

```bash
uv sync --all-packages
uv run pytest services/{{ cookiecutter.service_slug }}
```

## Run in the stack

Add this fragment to the top-level `compose.yaml` `include:` list, then:

```bash
docker compose up -d {{ cookiecutter.service_slug }}
```

See the [Building a module](../../docs/developer/building-a-module.md) guide for
the full contract (tools, events, manifest, secrets, tenant scoping).
