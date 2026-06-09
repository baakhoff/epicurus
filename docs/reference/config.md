# Reference: `config`

`epicurus_core.config` — environment-driven, **non-secret** configuration shared
by every service. Secrets do not belong here; they come from
[`SecretStore`](secrets.md).

## `CoreSettings`

```python
class CoreSettings(pydantic_settings.BaseSettings)
```

The base settings every service shares. Loads from environment variables and an
optional local `.env` (case-insensitive; unknown vars ignored). **Subclass it** to
add service-specific fields.

### Fields

| Field | Env var | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `service_name` | `SERVICE_NAME` | `str` | `"epicurus"` | Identity of the running service. |
| `app_env` | `APP_ENV` | `Environment` | `"local"` | `local` / `staging` / `production`. |
| `log_level` | `LOG_LEVEL` | `LogLevel` | `"info"` | `debug` … `critical`. |
| `json_logs` | `JSON_LOGS` | `bool \| None` | `None` | Force JSON logs; `None` = decide by env. |
| `default_tenant_id` | `DEFAULT_TENANT_ID` | `str` | `"local"` | Tenant for self-host (validated). |
| `nats_url` | `NATS_URL` | `str` | `nats://localhost:4222` | Event bus — see [`events`](events.md). |
| `openbao_url` | `OPENBAO_URL` | `str` | `http://localhost:8200` | Secrets — see [`secrets`](secrets.md). |
| `openbao_token` | `OPENBAO_TOKEN` | `str \| None` | `None` | Bootstrap token, injected at runtime. |

### Properties

- **`is_production -> bool`** — `True` when `app_env == "production"`.
- **`use_json_logs -> bool`** — whether logs render as JSON: the `json_logs`
  override when set, otherwise `True` everywhere except `local`.

### Validation

`default_tenant_id` is validated with [`is_valid_tenant_id`](tenancy.md); an
invalid value raises `pydantic.ValidationError` when settings load.

### Example

```python
from epicurus_core import CoreSettings

class GreeterSettings(CoreSettings):
    greeting: str = "Hello"          # add your own fields

settings = GreeterSettings()         # reads env / .env
settings.greeting, settings.is_production, settings.use_json_logs
```

## Type aliases

```python
Environment = Literal["local", "staging", "production"]
LogLevel = Literal["debug", "info", "warning", "error", "critical"]
```
