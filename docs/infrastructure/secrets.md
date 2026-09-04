# Secrets (OpenBao)

OpenBao is the single source of truth for credentials in epicurus. Every secret —
provider API keys, OAuth tokens, per-module config — lives here, namespaced by tenant.
Nothing sensitive is stored in environment variables, git, or on the service filesystem.

## What it is

[OpenBao](https://openbao.org) is an open-source fork of HashiCorp Vault. It runs as a
sidecar container on the internal `epicurus` Docker network. The core service holds the
only token that can read from it; modules never touch OpenBao directly — they call the
core's platform API which proxies secret access.

## Container & storage

| Property | Value |
| --- | --- |
| Image | `openbao/openbao:2.2.0` |
| Host port | `${BIND_ADDRESS:-127.0.0.1}:${OPENBAO_PORT:-8200}:8200` |
| Storage backend | File (named Docker volume `openbao-data`) |
| Config mount | `infra/compose/openbao-config.hcl` → `/openbao/config/config.hcl` |

Data survives container and stack restarts via the `openbao-data` named volume.

## First-time bootstrap

After the first `docker compose up` (or `task infra-up`), run the bootstrap script once:

```bash
sh infra/compose/scripts/openbao-bootstrap.sh
```

The script (requires Docker Compose to be running):

1. Initialises OpenBao with a single Shamir key share (1-of-1 threshold).
2. Unseals the vault using the generated key.
3. Enables the KV v2 secrets engine at the `secret/` mount.
4. Creates the `epicurus-core` policy (read/write access to `secret/data/tenants/*`).
5. Creates a **periodic** app token scoped to that policy — see
   [Token lifetime & renewal](#token-lifetime--renewal).
6. Generates strong **NATS role passwords** (`core` / `module` / `sys`), records them at
   `secret/tenants/<tenant>/nats`, and adds them to the secrets file (#50, ADR-0066).
7. Writes `OPENBAO_UNSEAL_KEY`, `OPENBAO_TOKEN`, and `NATS_{CORE,MODULE,SYS}_PASSWORD` to
   `infra/compose/.env.secrets` (gitignored).

After the script completes, add the values to your `.env`:

```bash
# infra/compose/.env.secrets is generated — never commit it
source infra/compose/.env.secrets   # or copy the two lines into .env manually
```

Then bring the full stack up (or restart it):

```bash
docker compose up -d
```

> **Keep `infra/compose/.env.secrets` safe.** It contains the single unseal key and the
> root token. Store it outside the repository (e.g. a password manager). If lost, the
> vault data is not recoverable without the unseal key.

## Auto-unseal on restart

An `openbao-unseal` sidecar container runs alongside OpenBao. It polls the vault's seal
status every 30 seconds and calls `bao operator unseal` automatically whenever the vault
is sealed (i.e. after any restart). It reads `OPENBAO_UNSEAL_KEY` from the environment.

Services that depend on a live vault — notably `core-app` — declare
`condition: service_healthy` on the `openbao` service. The `openbao` healthcheck succeeds
only when the vault is active (unsealed), so those services start only after the unseal
sidecar has done its job.

## Secret paths & policy

All secret paths are tenant-scoped by `epicurus-core`'s `scope_secret_path()` helper:

```
secret/data/tenants/<tenant_id>/<base>
```

The `epicurus-core` policy grants the app token `create / read / update / delete / list`
on `secret/data/tenants/*` and `list / delete` on `secret/metadata/tenants/*`. Because the
token is issued with `-no-default-policy`, the policy also grants `read` on
`auth/token/lookup-self` and `update` on `auth/token/renew-self` — the core's secret client
verifies its token with a lookup-self on connect, and renews the lease with a renew-self on a
schedule; both would otherwise be denied.

## Token lifetime & renewal

The app token is **periodic** (`-period=768h -orphan -no-default-policy`). Periodic is the
only shape of non-root token with an unlimited *lifetime*: it has no explicit max TTL, and
every renewal resets its lease to the full period. `-orphan` keeps it alive across a future
rotation of the root token that minted it.

Each *lease* still expires, so something has to renew inside the period. `core-app` does:
`SecretStore.run_token_renewal()` starts from its lifespan and calls `auth/token/renew-self`
once a day (`OPENBAO_RENEW_INTERVAL_S`). A daily cadence against a 768h period means renewal
can fail for weeks before it becomes an outage — so a failure is a bounded warning
(`openbao token renewal failed…`, logged on the first failure and every 7th after that),
never a crash. `messaging` holds the *same* token, so the core's single renewer covers it too.

The core additionally self-heals a `403`: it drops the cached client, re-authenticates, and
retries the operation once. A token rotated through `OPENBAO_TOKEN_FILE` therefore takes
effect without restarting the process. A value passed through `OPENBAO_TOKEN` is fixed for the
life of the container — changing it means **recreating** the container, not restarting it.

A dev-mode root token is not renewable and never expires; the loop detects that with a
lookup-self at startup and exits quietly.

> **Migration — deployments bootstrapped before this landed.** Their token is *not* periodic:
> it is a plain service token on the system default lease TTL (768h), so every secret read
> starts failing with `permission denied` exactly 32 days after bootstrap. The fix prevents
> future expiry; it cannot revive a token that has already expired. Mint a periodic
> replacement with the root token from `infra/compose/.env.secrets`:
>
> ```sh
> docker compose exec -e BAO_TOKEN=$OPENBAO_ROOT_TOKEN openbao \
>     bao token create -display-name=epicurus-core-app -policy=epicurus-core \
>     -no-default-policy -orphan -period=768h
> ```
>
> Put the new `client_token` in `.env` as `OPENBAO_TOKEN`, then **recreate** the services that
> hold it — `docker compose up -d --force-recreate core-app messaging`. A plain `restart`
> keeps the old environment and will not pick the new token up.
>
> Verify with `bao token lookup`: `period` should read `768h` and `explicit_max_ttl` `0`.

Registered base paths (set by the core service at runtime):

| Path | Contents |
| --- | --- |
| `llm/anthropic` | Anthropic API key |
| `llm/openai` | OpenAI API key |
| `llm/xai` | xAI API key |
| `llm/deepseek` | DeepSeek API key |
| `llm/google` | Google API key |
| `llm/openrouter` | OpenRouter API key — one key reaching many vendors' chat **and** embedding models |
| `llm/custom` | Custom provider endpoint + key |
| `modules/<name>/config` | Per-module config blob |
| `oauth/clients/<provider>` | Operator-provisioned OAuth client (`client_id`, `client_secret`) |
| `oauth/tokens/<provider>` | User-granted OAuth tokens (`access_token`, `refresh_token`, `expires_at`, `scope`, `token_type`) |
| `nats` | NATS role passwords (`core`, `module`, `sys`) — the authenticated bus's source of truth (written by the bootstrap, ADR-0066). See [NATS](nats.md). |

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `OPENBAO_UNSEAL_KEY` | *(required after bootstrap)* | Shamir unseal key; generated by bootstrap script |
| `OPENBAO_TOKEN` | *(required after bootstrap)* | App token for core-app; generated by bootstrap script |
| `OPENBAO_TOKEN_FILE` | *(unset)* | Alternative to `OPENBAO_TOKEN` — a path to a mounted file (e.g. a Docker secret). Re-read whenever the client re-authenticates, so rotation needs no restart |
| `OPENBAO_RENEW_INTERVAL_S` | `86400` | How often the core renews the token's lease. `0` disables the renewal loop |
| `OPENBAO_URL` | `http://localhost:8200` | Used by host-side tooling; containers use `http://openbao:8200` |
| `OPENBAO_PORT` | `8200` | Host-side published port |
| `BIND_ADDRESS` | `127.0.0.1` | Published port bind address (shared with all data-plane services) |

## Dependencies

- No external network access — all traffic is on the internal `epicurus` Docker network.
- `core-app` is the primary consumer; it builds a `SecretStore` at startup from
  `OPENBAO_TOKEN` and `OPENBAO_URL` (via `epicurus-core`'s `SecretStore` and `CoreSettings`),
  and owns the renewal loop. `messaging` builds its own `SecretStore` from the same token for
  bridge credentials; every other module reaches secrets through the core's platform API.

## Run & extend

```bash
# Bring up the data plane (OpenBao + all infra services):
docker compose -f infra/compose/docker-compose.yml up -d

# First-time bootstrap (once per fresh volume):
sh infra/compose/scripts/openbao-bootstrap.sh

# Check status from the host:
curl http://localhost:8200/v1/sys/health

# Check seal status:
docker compose -f infra/compose/docker-compose.yml exec openbao bao status

# List secrets for the default tenant (requires OPENBAO_TOKEN):
docker compose -f infra/compose/docker-compose.yml exec -e VAULT_TOKEN=$OPENBAO_TOKEN \
    openbao bao kv list secret/tenants/local
```

To add a new secret path for a new module, register it in `epicurus-core`'s policy and
the module doc — the existing `epicurus-core` policy's wildcard already covers any path
under `secret/data/tenants/*`.
