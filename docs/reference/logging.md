# Reference: `logging`

`epicurus_core.logging` — structured logging via [structlog](https://www.structlog.org),
consistent across services. Console-rendered in local dev, JSON otherwise.

## `configure_logging`

```python
def configure_logging(settings: CoreSettings) -> None
```

Configure structlog for the process. Call once at startup. It:

- sets the level from `settings.log_level`,
- renders JSON or console output per `settings.use_json_logs`,
- binds `service = settings.service_name` onto every line.

Call it once, at startup, before creating loggers — calling it again reconfigures
the process and resets the bound context.

## `get_logger`

```python
def get_logger(*name: str, **initial_values: Any) -> structlog.typing.FilteringBoundLogger
```

Return a bound logger. Optionally pass a logger name and initial bound key/values.
Call `configure_logging` once first.

### Example

```python
from epicurus_core import configure_logging, get_logger

configure_logging(settings)
log = get_logger(__name__)
log.info("request handled", path="/health", ms=3)
```

### Correlation

Tenant/request context is carried via `structlog.contextvars`. Bind values once
and they appear on every line within that context:

```python
import structlog
structlog.contextvars.bind_contextvars(tenant="acme", request_id="abc123")
```
