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
uv run mypy -p epicurus_core
uv run pytest
```

CI additionally runs a secret scan (gitleaks) and validates the compose file.
