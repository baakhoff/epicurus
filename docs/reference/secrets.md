# Reference: `secrets`

`epicurus_core.secret_store` — tenant-scoped secret access in OpenBao (KV v2),
built on the [`hvac`](https://hvac.readthedocs.io) client. Paths are scoped via
[`scope_secret_path`](tenancy.md) (`tenants/<tenant>/<base>`), so a module only
reaches its own tenant's secrets.

## `SecretStore`

```python
class SecretStore:
    def __init__(self, url="http://localhost:8200", token=None, *, mount_point="secret")
    @classmethod
    def from_settings(cls, settings: CoreSettings) -> SecretStore
```

### Methods (all async)

| Method | Description |
| --- | --- |
| `get(path, tenant_id=None) -> dict[str, Any]` | Read a secret's data. Raises [`SecretError`](#secreterror) if it does not exist. |
| `set(path, data, tenant_id=None) -> None` | Create or update a secret. |
| `delete(path, tenant_id=None) -> None` | Delete a secret and all its versions. |

`hvac` is synchronous, so calls run in a worker thread to keep this API async.

Authentication is verified **once**, when the underlying client is first built;
afterwards calls go straight to the backend (a token revoked later still fails
loudly as a `SecretError`). After rotating the token, construct a new store.
`from_settings` resolves the token from `OPENBAO_TOKEN` or, failing that, from
the file named by `OPENBAO_TOKEN_FILE` (e.g. a mounted Docker secret) — see
[`config`](config.md).

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
