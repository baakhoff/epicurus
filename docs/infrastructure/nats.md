# NATS — authenticated event backbone

NATS is the data-plane message bus: tenant-scoped pub/sub and request/reply between the
core and every module, plus JetStream (enabled, persistence is a follow-up). The server
**requires authentication** — it rejects un-credentialed clients (#50, ADR-0066). This
page documents the auth model, how services authenticate, and how credentials flow.

Subjects stay tenant-scoped (`<tenant>.<base>`, see
[`epicurus_core.tenancy.scope_subject`](../reference/events.md)). Today a single
application account holds every tenant's subjects, so the `<tenant>.` prefix is the
(cooperative) tenant boundary; **enforced** per-tenant isolation (one NATS account per
tenant) is the deferred SaaS-track step (see [Version policy](#deferred-per-tenant-isolation)).

## The problem it solves

Before this, the NATS server ran open: `command: ["-js", ...]` with no auth, so any
client that could reach `nats://nats:4222` could publish and subscribe across **all**
subjects (`*.>`). On the internal-only Docker network that is acceptable for a single,
private box, but it is a hard pre-SaaS blocker: there is nothing on the bus itself
distinguishing the core from a module, or one tenant from another. This change closes
the authentication hole and establishes the **account structure** the per-tenant model
later builds on.

## The auth model

The server config is [`infra/compose/nats-server.conf`](../../infra/compose/nats-server.conf)
(mounted read-only into the container; it replaces the old `-js -sd /data -m 8222` flags —
JetStream, the store dir, and the monitoring port all live in the file now). It defines
two accounts and three **role** users:

| User | Account | Permissions | Used by |
| --- | --- | --- | --- |
| `core` | `APP` | publish/subscribe `>` (full bus) | core-app (the orchestrator: agent loop, LLM gateway, every module request/reply + event) |
| `module` | `APP` | publish/subscribe `*.>` + `_INBOX.>` | every module sidecar (echo, storage, knowledge, websearch, calendar, mail, tasks, notes, messaging) |
| `sys` | `SYS` | — (system account) | operations / monitoring via the `nats` CLI |

- **`APP`** is the single application account for v1. Every tenant-scoped subject
  (`<tenant>.<base>`, always ≥ 2 tokens) matches the module allow-list `*.>`; `_INBOX.>`
  carries request/reply responses (nats-py's default inbox prefix). The `core` user is
  unrestricted because it touches the whole bus.
- **`SYS`** is bound as the `system_account`, so neither `core` nor `module` (both in
  `APP`) can reach the `$SYS.>` server-control subjects — operations are isolated from
  application traffic.

This mirrors the rest of the data plane's **per-role** credential posture — one
`POSTGRES_PASSWORD`, one MinIO root credential, one OpenBao app token — rather than a
distinct credential per service. Per-service (and per-tenant) credential isolation is the
deferred hardening described below.

## How services authenticate

All NATS traffic goes through `epicurus_core.events.EventBus`, which now forwards a
`user`/`password` to `nats.connect`. Services build the bus from settings:

```python
bus = EventBus.from_settings(settings)   # reads NATS_USER / NATS_PASSWORD from the env
```

`CoreSettings.nats_user` / `nats_password` default to `None`, so the EventBus still
connects **anonymously** when no credentials are set — which keeps it usable against an
un-authenticated server (the integration testcontainers). In the stack, each service's
compose fragment sets the role:

```yaml
# core-app
NATS_USER: core
NATS_PASSWORD: ${NATS_CORE_PASSWORD:-epicurus-dev}
# every module (and the service template)
NATS_USER: module
NATS_PASSWORD: ${NATS_MODULE_PASSWORD:-epicurus-dev}
```

So an operator manages only the **three role passwords**; compose maps each to the right
`NATS_USER`. A new module scaffolded with `task new-module` authenticates as `module`
automatically — the template carries both env lines, so no wiring step is required.

## Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `NATS_PORT` | `4222` | Published client port (bound to `BIND_ADDRESS`). |
| `NATS_MONITOR_PORT` | `8222` | Published HTTP monitoring port. **Unauthenticated** — independent of client auth, so the host readiness probe (`/healthz`) keeps working. |
| `NATS_CORE_PASSWORD` | `epicurus-dev` | Password for the `core` role (core-app). |
| `NATS_MODULE_PASSWORD` | `epicurus-dev` | Password for the `module` role (every module). |
| `NATS_SYS_PASSWORD` | `epicurus-dev` | Password for the `sys` role (monitoring/ops). |
| `NATS_USER` (per service) | — | Set in each service's compose to the role it authenticates as (`core` / `module`). |
| `NATS_PASSWORD` (per service) | — | Set in each service's compose, mapped to the matching role password var. |

The same role password must reach **both** the nats server (it authenticates clients
against it) and the services (they present it). Compose's `:-epicurus-dev` defaults make a
plain `docker compose up` work out of the box; the `runtime-smoke` gate relies on them.

### Credentials in real deployments

`epicurus-dev` is a weak default safe only on a private box. The OpenBao bootstrap
([`infra/compose/scripts/openbao-bootstrap.sh`](../../infra/compose/scripts/openbao-bootstrap.sh))
generates strong per-role passwords, records them in OpenBao at
`secret/tenants/<tenant>/nats` (source of truth), and writes them to
`infra/compose/.env.secrets` for the operator to load into `.env`. The runtime path is
**env injection** (the NATS server reads its static config from the environment; it cannot
fetch from OpenBao itself) — matching how every data-plane credential, including OpenBao's
own token, is delivered. See [Secrets](secrets.md).

## Data model

None. NATS owns the `nats-data` named volume for JetStream's store (`/data`), but no
durable streams are created by app code yet — the bus is pub/sub + request/reply.

## Dependencies

The `nats-data` named volume and the mounted `nats-server.conf`. No other dependency; the
data plane brings NATS up early and the core + modules connect to it on startup.

## Deferred: per-tenant isolation

The cross-tenant boundary that matters for multi-tenant SaaS is **account-per-tenant**:
NATS accounts are fully isolated subject spaces, so promoting each tenant to its own
account makes cross-tenant pub/sub impossible at the server, not merely discouraged by
subject naming. That requires per-tenant credential provisioning and (for connection-time
tenant scoping) the decentralized-JWT or auth-callout model — a substantial step that only
pays off under real multi-tenancy. This change deliberately lands the **authentication
gate** and the account structure first; ADR-0066 records the model and the deferral.

Per-service (rather than per-role) credentials are part of the same deferred hardening —
they deliver revocability and isolation alongside per-tenant accounts, and are a
cross-cutting change across the whole data plane (postgres/minio/nats), not a NATS-only one.

## Run & extend

- **Local:** `docker compose up -d` (or `task up`) — the `:-epicurus-dev` defaults apply.
  Reach the bus from the host on `nats://localhost:4222`; the monitoring UI is
  `http://localhost:8222`.
- **Validate the config** after editing `nats-server.conf`:
  `docker run --rm -v "$PWD/infra/compose/nats-server.conf:/c.conf:ro" nats:2.10 -c /c.conf -t`
  (the `-t` flag checks the config and exits).
- **Add a permission / role:** edit `nats-server.conf` (the `MODULE_PERMISSIONS` variable
  and the `accounts` block). Keep app subjects tenant-scoped so `*.>` keeps covering them.
- **Rotate a password:** set a new `NATS_*_PASSWORD` in `.env` (and OpenBao), then restart
  the `nats` service and the affected services so both sides pick up the new value.
- **Verify end to end:** `task smoke` boots the whole stack — every service authenticates
  with its role and is discovered through the core, which is the integration proof that the
  permissions don't block legitimate traffic.
