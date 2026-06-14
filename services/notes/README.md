# Notes

Author Obsidian-style notes into a private, **attach-only** RAG collection.

A note is written in the ε editor (the core-rendered **Notes** page, `editor`
archetype), saved to Postgres (the source of truth), and indexed into its own
tenant-scoped `<tenant>__notes` Qdrant collection. Notes are **attach-only**: the
module registers **no MCP tools**, so the agent can read a note only when the user
**attaches** it to a message (`attachable`, ADR-0019). This is the line between
**Notes** (you author + manually attach) and **Knowledge** (your vault,
agent-retrievable). Embeddings come from the core (no model key lives here).

Full reference: [docs/services/notes.md](../../docs/services/notes.md).

## Already wired in

`task new-module` scaffolded this module **and** wired it into the stack, so there
are no manual edits to make before it runs (the runtime smoke gate would fail the
build if any of these were missing):

- registered in the root `pyproject.toml` — `[tool.mypy] packages` and
  `[tool.ruff.lint.isort] known-first-party`;
- included in the top-level `compose.yaml` `include:` list;
- registered with the core as `http://notes:8080` in
  `module_urls` (`services/core-app/src/epicurus_core_app/settings.py`);
- assigned the unique host port `8092` — see the registry in
  [docs/reference/ports.md](../../docs/reference/ports.md); override with the
  `NOTES_PORT` env var;
- reset in the smoke CI override (`infra/ci/compose.ci.yaml`) so it leaks no host
  port when the smoke stack runs beside your dev stack.

> Scaffolded with a bare `cookiecutter` call instead of `task new-module`? Then do
> those steps by hand — `task smoke` boots the stack and fails if any is missing.
> The [Building a module](../../docs/developer/building-a-module.md) guide lists them.

## Develop

```bash
uv sync --all-packages
uv run pytest services/notes
docker compose up -d notes
```

### If the module calls back into the core

For LLM/embedding access or connected-account (OAuth) tokens, uncomment
`PLATFORM_URL` in this fragment's `environment:` and use `epicurus_core`'s
`PlatformClient` — modules never hold model keys or client secrets (ADR-0010,
ADR-0016). Fetch OAuth tokens only through `PlatformClient.get_oauth_token`. For
per-tenant secrets, register a base path under the `epicurus-core` OpenBao policy
(see [docs/infrastructure/secrets.md](../../docs/infrastructure/secrets.md)); the
core proxies secret access so the module never talks to OpenBao directly.

See the [Building a module](../../docs/developer/building-a-module.md) guide for
the full contract (tools, events, manifest, secrets, tenant scoping).
