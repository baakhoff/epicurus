# Reference: `observability`

`epicurus_core.observability` — the shared operational HTTP surface every service
exposes: `/health` and `/metrics`.

## `create_ops_router`

```python
def create_ops_router(service_name: str, registry: CollectorRegistry = REGISTRY) -> fastapi.APIRouter
```

Build a router exposing:

- **`GET /health`** — returns a [`HealthResponse`](#healthresponse).
- **`GET /metrics`** — Prometheus exposition for `registry` (defaults to the
  process-wide registry).

## `add_ops_routes`

```python
def add_ops_routes(app: fastapi.FastAPI, service_name: str, registry: CollectorRegistry = REGISTRY) -> None
```

Mount the ops router onto an existing FastAPI app.

## `HealthResponse`

Pydantic model returned by `GET /health`:

`status: str` · `service: str` · `version: str`.

### Example

```python
from fastapi import FastAPI
from epicurus_core import add_ops_routes

app = FastAPI()
add_ops_routes(app, service_name="greeter")
# GET /health  -> {"status": "ok", "service": "greeter", "version": "..."}
# GET /metrics -> Prometheus text exposition
```
