# Configuration

epicurus separates **non-secret configuration** from **secrets**.

## Non-secret configuration (`.env`)

Application configuration comes from environment variables (and an optional local
`.env`). Copy the example and edit it:

```bash
cp .env.example .env
```

Current keys:

| Key | Default | Meaning |
| --- | --- | --- |
| `APP_ENV` | `local` | `local`, `staging`, or `production`. Also decides JSON vs. console logs. |
| `LOG_LEVEL` | `info` | `debug` / `info` / `warning` / `error`. |
| `DEFAULT_TENANT_ID` | `local` | The tenant used for a single-tenant / self-host install. |
| `NATS_URL` | `nats://localhost:4222` | The event bus. On the internal Docker network this is `nats://nats:4222`. |

!!! warning "Never commit `.env`"
    `.env` is gitignored. Real secrets do **not** belong in it.

## Data-plane configuration (`infra/compose/.env`)

The compose stack reads its own optional `infra/compose/.env` for host ports and
**dev-only** credentials. Copy the example to override defaults:

```bash
cp infra/compose/.env.example infra/compose/.env
```

The default Postgres password and OpenBao root token there are **for a local
Tailscale-only box only**.

## Secrets

Secrets (API keys, OAuth client secrets, tokens) are stored in **OpenBao**, not
in environment files or git. The compose stack runs OpenBao in dev mode for local
development; a production deployment uses a non-dev OpenBao. Modules fetch their
own secrets from OpenBao at runtime.

!!! info "AI access"
    Modules do not hold model API keys. All AI/LLM access goes through the core,
    which owns the model keys and routing — so there is one place to configure
    and secure them.
