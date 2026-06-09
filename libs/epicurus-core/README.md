# epicurus-core

Shared contract and runtime used by every epicurus service.

**Status:** scaffolding. This release ships only package metadata and a version
marker so the workspace, types, and tests are wired up. The real building blocks
land in follow-up changes:

- configuration loading
- structured logging (structlog) + OpenTelemetry tracing
- NATS client (events backbone)
- MCP base classes (the module tool contract)
- OpenBao client (secret access)
- **tenant context** primitive (scopes every row / subject / collection / secret
  path / bucket — see [AGENTS.md](../../AGENTS.md) non-negotiables)
- `/health` + `/metrics` helpers

Importing:

```python
import epicurus_core

epicurus_core.__version__
```
