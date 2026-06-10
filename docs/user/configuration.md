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

> **Warning — never commit `.env`.** It is gitignored. Real secrets do **not**
> belong in it.

## Host ports & dev credentials

Which `.env` applies depends on **how you start the stack**:

- **Full stack** — `docker compose up` from the repo root reads the **root `.env`**.
  It governs everything (data plane + edge + observability + modules); the host
  ports and dev credentials are listed, commented with their defaults, in
  [`.env.example`](../../.env.example).
- **Data plane only** — `docker compose -f infra/compose/docker-compose.yml …`
  reads **`infra/compose/.env`** (copy from `infra/compose/.env.example`).

```bash
cp .env.example .env                               # full stack
cp infra/compose/.env.example infra/compose/.env   # data-plane-only stack
```

The default Postgres password and OpenBao root token are **dev-only**, for a local,
private deployment.

## Secrets

Secrets (API keys, OAuth client secrets, tokens) are stored in **OpenBao**, not
in environment files or git. The compose stack runs OpenBao in dev mode for local
development; a production deployment uses a non-dev OpenBao. Modules fetch their
own secrets from OpenBao at runtime.

> **AI access.** Modules do not hold model API keys. All AI/LLM access goes
> through the core, which owns the model keys and routing — so there is one place
> to configure and secure them.
