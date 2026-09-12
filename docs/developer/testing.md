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
  @pytest.mark.timeout(120)  # raise it for this test
  @pytest.mark.timeout(0)  # or disable the deadline entirely
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
task migrate:check            # only if you touched a model — see below
```

CI additionally runs a secret scan (gitleaks), validates the compose file, renders
and schema-checks the Helm chart, lints every shell script (see below), checks
every docs cross-reference (see below), lints the observability config (see
below), proves every migrated service's schema upgrades (see below), and boots the
whole stack — twice, once on Docker Compose and once on Kubernetes (see below).

## Schema migration gate

Nothing in this repo exercised a schema **upgrade** until the `migrations` job
existed: the unit tests build tables from the models on SQLite, and
`runtime-smoke` / `k8s-smoke` boot from an empty Postgres, so between them they
only ever prove the fresh-install path. Both failures that motivated adopting
Alembic (#214, #218) were visible only on a database that had been running a while.

The **`migrations` CI job** runs against a real `postgres:17` service container and,
for every service with a migration environment, on a throwaway database per arm:

- an empty database reaches head, and then matches its models exactly
  (`alembic check`) — **this is the "a model changed and nobody wrote a revision"
  gate**;
- a second run is a no-op, which is what every restart does;
- a database built the old `create_all` way is adopted and still matches its models;
- the same, with the columns the additive reconcile was responsible for stripped out
  first — the #214 / #218 state the baseline has to repair.

Services are discovered by glob inside `scripts/migrate.py`, so a service adopting
migrations is covered from its own commit with no workflow edit.

Locally, the cheap half of that is one command — a throwaway SQLite database, about
a second:

```bash
task migrate:check -- storage   # one service
task migrate:check              # every migrated service
```

Run it whenever you touch a model. The rest (authoring a revision, what the startup
paths do, the backfill rule) is in **[Schema migrations](migrations.md)**.

**It is not a required check yet.** As with `k8s-smoke`, the bar is *required once
green two weeks running* — until then read a red one and investigate it, but it does
not block a merge on its own.

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

## Docs link-check gate

A docs page path was once referenced from shipped, operator-facing web UI copy and
a compose comment while the page it named didn't exist in the public tree — nothing
caught it, and a generic markdown-link checker wouldn't catch the next one either;
it never looks inside a `.tsx` file or a compose comment (#692). `scripts/check_docs_links.py`
(the **docs-linkcheck** CI job) checks, over the whole repo:

1. every relative markdown link between pages under `docs/` resolves to a real
   file, anchors (`#heading-slug`) included — slugs approximate GitHub's
   heading-to-anchor algorithm;
2. a repo-relative doc path quoted in shipped source (web UI copy, compose
   comments, other top-level READMEs, `.env.example`) resolves to a real file.

Test fixtures commonly use plausible-looking-but-synthetic doc-shaped paths (a
mocked file-move target, a fake storage key) that were never meant to resolve —
anything under a `test`/`tests` path, or named `*.test.*`/`test_*.py`/`*_test.py`,
is excluded from the second check. `CHANGELOG.md` narrates *past* fixes (a now-gone
path mentioned as history, not a live reference) and is excluded too.

To run it locally (stdlib-only, no dependencies):

```bash
python3 scripts/check_docs_links.py
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

### One set of assertions, two runtimes

The integration last mile is written **once**, in `infra/ci/smoke-assert.sh`, and
sourced by both smoke gates. That file holds everything that is true of an
epicurus deployment whatever it is deployed on — module discovery, status through
the core, an MCP round-trip, the attachment picker, the event spine, automations,
and a secret surviving a vault restart. Each gate supplies the runtime-specific
half as shell functions (`http`, `restart_openbao`, `restart_core_app`) and calls
`smoke_assert`.

**Add a new integration assertion there**, not in a gate script, so both runtimes
are held to it. `tests/test_smoke_gates.py` fails if a gate inlines one of them
again, and if either gate stops sourcing or running the shared file.

## Kubernetes smoke gate

`chart-validate` renders the Helm chart and validates it against the real
Kubernetes API schemas, which catches a misspelled field or a wrong `apiVersion` —
and says nothing at all about whether the stack comes up. A bad probe, an
unwritable mount, an RBAC grant that is one verb short: all render perfectly.

The **`k8s-smoke` CI job** (`infra/ci/k8s-smoke.sh`) closes that gap the way
`runtime-smoke` closes it for Compose. It creates a [kind](https://kind.sigs.k8s.io)
cluster on the runner, builds the service images from the checkout and
`kind load`s them, `helm install --wait`s the chart with `infra/ci/values-ci.yaml`,
and then reaches the services through a curl pod in the namespace — the same way
the core, the modules and the web shell reach each other — to run
`smoke-assert.sh`. On top of the shared assertions it proves three things only a
cluster can:

- the OpenBao **bootstrap Job** completes, its app token is periodic, and the
  unseal loop brings a deleted vault pod back unsealed;
- the web shell proxies `/platform/` to the core through the **pod's own DNS**
  (the resolver nginx derives at container start, #891 — the Docker address it
  used before does not exist in a pod);
- `CONTAINER_RUNTIME=auto` resolves to the **Kubernetes arm** inside a pod, and a
  confirmed module removal scales that module's Deployment to zero through the
  namespace-scoped Role the chart renders (#891, ADR-0134) — the first exercise of
  that code against a real API server.

`infra/ci/values-ci.yaml` overrides as little as possible, so the gate boots the
shape an operator installs: only Ollama is off (a multi-gigabyte image and a 4Gi
request for a model nothing here uses) and the PVCs are small. `tests/test_smoke_gates.py`
fails if it ever disables a module or a data-plane piece.

Run it locally, exactly as CI does — it needs `kind`, `kubectl`, `helm` and a lot
of free disk:

```bash
sh infra/ci/k8s-smoke.sh              # create a cluster, boot, assert, delete
KEEP_UP=1 sh infra/ci/k8s-smoke.sh    # leave the cluster up to poke at
```

Note the docker-light rule in `AGENTS.md`: this is a hosted gate, and running it
locally is for diagnosing a red CI run, not routine development.

**It is not a required check yet.** The bar set in #894 is *required once green
two weeks running* — until then, read a red one and investigate it, but it does
not block a merge on its own.
