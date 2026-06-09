# templates

Scaffolding templates.

- **service-template** *(coming in Phase 0)* — a cookiecutter that generates a
  new module with the contract pre-wired: an MCP server, NATS pub/sub, the
  `/health` + `/metrics` surface, OpenBao secret access, and the tenant context.

The goal: a new "block" starts as a one-command scaffold, not a blank directory.
See the module contract in [AGENTS.md](../AGENTS.md) and
[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md).
