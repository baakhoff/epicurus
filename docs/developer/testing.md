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

## Per-test timeout

Every test runs under a **60-second deadline** (`pytest-timeout`, set in
`pyproject.toml`). A test that exceeds it fails with a dumped stack instead of
hanging the run, so a real async deadlock — an `await` inside `except
CancelledError`, a task cancelled before its first step — surfaces loudly in CI
rather than blocking the gate until the job's hard timeout. The portable `thread`
method is used (signal-based timeouts are POSIX-only; development runs on Windows).

Healthy tests finish in well under a second, so the deadline only ever trips a
genuine hang. Two escape hatches keep legitimately slow tests green:

- **Integration tests get a larger budget** automatically — a cold testcontainers
  image pull can take longer than 60s. The repo-root `conftest.py` lifts the
  ceiling for any `@pytest.mark.integration` test that carries no timeout of its own.
- **Override per test** when one is genuinely slow:

  ```python
  @pytest.mark.timeout(120)   # raise it for this test
  @pytest.mark.timeout(0)     # or disable the deadline entirely
  def test_something_slow(): ...
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

CI additionally runs a secret scan (gitleaks), validates the compose file, lints
every shell script (see below), lints the observability config (see below), and
boots the whole stack (see below).

## Observability lint gate

The `runtime-smoke` gate boots the data plane and core, but not the observability
stack — so a broken Prometheus rule or invalid Alertmanager config passes CI and
only fails on `docker compose up`. The **observability-lint** CI job closes that
gap: it runs `promtool check config` and `promtool check rules` against
`infra/observability/prometheus/`, and `amtool check-config` against
`infra/observability/alertmanager/`, using the same image tags pinned in compose.

To run it locally (requires Docker):

```bash
docker run --rm \
  -v "$(pwd)/infra/observability/prometheus:/prometheus:ro" \
  prom/prometheus:v3.1.0 \
  promtool check config /prometheus/prometheus.yml

docker run --rm \
  -v "$(pwd)/infra/observability/prometheus/rules:/rules:ro" \
  prom/prometheus:v3.1.0 \
  promtool check rules /rules/epicurus-alerts.yml

docker run --rm \
  -v "$(pwd)/infra/observability/alertmanager:/alertmanager:ro" \
  prom/alertmanager:v0.27.0 \
  amtool check-config /alertmanager/alertmanager.yml
```

## Shell-lint gate

Nothing parsed a shell script with a POSIX shell before this gate existed: #675
shipped a bash array in a script whose every documented invocation is `sh` (dash
on the deploy box), and every check that ran it — CI included — used Git Bash's
`sh`, which *is* bash, so the parse error was invisible everywhere but the real
deploy box (#691). The **shell-lint** CI job runs `shellcheck` over every `*.sh`
in the repo (discovered via `git ls-files`, not a hardcoded list, so a new script
is covered automatically), with the shell it checks against inferred from each
script's own shebang — never assumed.

A script's shebang and its documented invocation must agree, and the script must
actually be written for that shell (no bash arrays under a `sh`/`dash` target). An
intentional shellcheck exception (e.g. a deliberately unquoted word-list variable
standing in for POSIX sh's missing arrays) gets a `# shellcheck disable=SCxxxx`
comment naming *why*, not a blanket suppression.

To run it locally:

```bash
uvx --from shellcheck-py shellcheck.exe --shell=bash infra/backups/backup.sh
uvx --from shellcheck-py shellcheck.exe --shell=sh infra/cd/reconcile.sh
# ...or just: dash -n <script> for a syntax-only check with no shellcheck install.
```

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
- each module is reachable through the core, one MCP tool round-trips, and an
  attachable module's chat-attachment picker round-trips through the core (ADR-0019).

Run it locally, exactly as CI does:

```bash
task smoke        # or: sh infra/ci/smoke.sh
```

It runs in its own compose project, network, and volumes with no published host
ports, so it is safe to run next to a dev stack and tears itself down at the end
(`KEEP_UP=1` leaves it up to inspect).
