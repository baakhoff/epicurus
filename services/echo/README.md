# echo

The **echo** module — the simplest possible epicurus module, used to prove the
module↔core contract end-to-end and to serve as the reference a new module is
modeled on.

It exercises both halves of the contract:

- **MCP tool** `echo(message)` — returns the message unchanged (the agent-facing
  tool surface).
- **NATS request/reply** on `echo.request` — replies with the request payload
  (the event path), tenant-scoped via `epicurus-core`.

Built entirely on `epicurus-core` (`EpicurusModule` + `EventBus`); see the
[Building a module](../../docs/developer/building-a-module.md) guide.

## Tests

```bash
uv run pytest services/echo                 # unit + integration (needs Docker)
uv run pytest services/echo -m "not integration"   # unit only
```
