# Testing

Tests are required for every change, and they run on every pull request.

## Running tests

```bash
uv run pytest
```

Async tests run automatically (pytest is in `asyncio_mode = auto`).

## Unit vs. integration

- **Unit tests** exercise logic in isolation and need nothing external.
- **Integration tests** run against real infrastructure spun up on demand with
  [testcontainers](https://testcontainers.com) (for example, a throwaway NATS
  container for the event client). They are marked `@pytest.mark.integration` and
  **require Docker**.

Run only unit tests (no Docker needed):

```bash
uv run pytest -m "not integration"
```

## What good tests look like

- Cover the contract, the edge cases, and the failure modes — not just the happy
  path.
- Integration tests manage their own isolated containers, so parallel runs don't
  collide.
- Keep them deterministic; don't depend on external network services.

## The full gate

A change is ready when all of these pass (this is exactly what CI runs):

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

CI additionally runs a secret scan (gitleaks), validates the compose file, and
boots the whole stack (see below).

## Runtime smoke gate

Lint, types, unit tests, and `compose config` all pass without ever starting the
stack — so integration defects (a module missing from `module_urls`, a pruned
image tag, a mis-mounted config, an OpenBao policy that fails the core's auth
check) sail through green and only surface on first boot. The **runtime smoke
gate** (`infra/ci/smoke.sh`, run as the `runtime-smoke` CI job) closes that gap: it
boots the data plane, runs the real OpenBao bootstrap, brings up core + every
module, and asserts the integration last mile —

- every container reaches a healthy state;
- the OpenBao bootstrap succeeds and a secret set through the core survives a
  vault restart;
- the core discovers every module (the set is derived from the compose `include:`
  list, so a new module is gated the moment it is wired in);
- each module is reachable through the core, and one MCP tool round-trips.

Run it locally, exactly as CI does:

```bash
task smoke        # or: sh infra/ci/smoke.sh
```

It runs in its own compose project, network, and volumes with no published host
ports, so it is safe to run next to a dev stack and tears itself down at the end
(`KEEP_UP=1` leaves it up to inspect).
