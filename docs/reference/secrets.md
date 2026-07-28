# Reference: `secrets`

`epicurus_core.secret_store` — tenant-scoped secret access in OpenBao (KV v2),
built on the [`hvac`](https://hvac.readthedocs.io) client. Paths are scoped via
[`scope_secret_path`](tenancy.md) (`tenants/<tenant>/<base>`), so a module only
reaches its own tenant's secrets.

## `SecretStore`

```python
class SecretStore:
    def __init__(
        self,
        url="http://localhost:8200",
        token=None,
        *,
        mount_point="secret",
        token_provider: Callable[[], str | None] | None = None,
        renew_interval_s: float = 86_400.0,
    )
    @classmethod
    def from_settings(cls, settings: CoreSettings) -> SecretStore
```

`token_provider` is called for the token every time the client is built, so a rotated
`OPENBAO_TOKEN_FILE` is picked up in place; `token` is the fixed-value alternative.
`renew_interval_s` is the cadence of `run_token_renewal` (`0` disables it).

### Methods (all async)

| Method | Description |
| --- | --- |
| `get(path, tenant_id=None) -> dict[str, Any]` | Read a secret's data. Raises [`SecretError`](#secreterror) if it does not exist. |
| `set(path, data, tenant_id=None) -> None` | Create or update a secret. |
| `delete(path, tenant_id=None) -> None` | Delete a secret and all its versions. |
| `renew_self() -> int` | Renew the token's own lease; returns the granted TTL in seconds. |
| `run_token_renewal() -> None` | Renewal loop — never returns; cancel the task to stop it. Exits immediately when renewal is disabled or the token is not renewable. |

`hvac` is synchronous, so calls run in a worker thread to keep this API async.

Authentication is verified **once**, when the underlying client is first built; afterwards
calls go straight to the backend. A `403` from any operation drops the cached client,
re-authenticates, and retries that operation **once** — so a renewed or rotated token takes
effect without a process bounce, and a genuinely dead token still fails loudly with a
`SecretError` that names token expiry as the likely cause. `from_settings` resolves the token
from `OPENBAO_TOKEN` or, failing that, from the file named by `OPENBAO_TOKEN_FILE` — see
[`config`](config.md).

### Token renewal

The app token is periodic: unlimited lifetime, but each lease expires after the period
(768h), so a long-lived process must renew it or every secret read starts failing with
`permission denied` one month after bootstrap. `core-app` starts `run_token_renewal()` from
its lifespan; renewal failures are bounded warnings (first, then every 7th) rather than a
crash, because the period leaves weeks of runway. Only one holder needs to renew — the lease
belongs to the token, not the client. See
[infrastructure/secrets](../infrastructure/secrets.md#token-lifetime--renewal).

## `SecretError`

Raised when a secret can't be read or written — missing, authentication failure,
or a backend error.

### Example

```python
from epicurus_core import SecretStore

store = SecretStore.from_settings(settings)
await store.set("google/oauth", {"client_id": "...", "client_secret": "..."}, tenant_id="local")
creds = await store.get("google/oauth", tenant_id="local")
```

> **Note:** Modules fetch their own secrets through `SecretStore` rather than
> reading keys from env or git. All AI/LLM model keys live in the core, never in
> modules.
