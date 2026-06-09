# Reference: `tenancy`

`epicurus_core.tenancy` — the tenant-scoping primitive. Every addressable resource
is namespaced by tenant, so the same code serves one tenant or many.

## Tenant id format

A valid tenant id is **1–63 characters, lowercase alphanumeric and hyphens, with
no leading or trailing hyphen** — deliberately strict so one id is safe across
NATS subjects, Qdrant collections, object buckets, and secret paths.

## Validation

```python
def is_valid_tenant_id(tenant_id: str) -> bool
def validate_tenant_id(tenant_id: str) -> str   # returns it, or raises TenantError
```

## Current-tenant context

A context-local "current tenant" (via `contextvars`) so the tenant need not be
threaded through every call.

```python
def set_current_tenant(tenant_id: str) -> contextvars.Token   # bind; returns a reset token
def reset_current_tenant(token: contextvars.Token) -> None    # restore the previous value
def current_tenant() -> str                                   # the bound tenant, or raises TenantError
```

## Scoping helpers

Each builds a tenant-namespaced name. When `tenant_id` is omitted, the **current
tenant** is used (raising `TenantError` if none is set).

| Function | Result | Used for |
| --- | --- | --- |
| `scope_subject(base, tenant_id=None)` | `<tenant>.<base>` | NATS subjects |
| `scope_collection(base, tenant_id=None)` | `<tenant>__<base>` | Qdrant collections |
| `scope_secret_path(base, tenant_id=None)` | `tenants/<tenant>/<base>` | OpenBao paths |
| `scope_bucket(base, tenant_id=None)` | `<tenant>-<base>` | object-storage buckets |

## `TenantError`

Raised when a tenant id is invalid, or when a current tenant is required but none
is set.

### Example

```python
from epicurus_core import scope_subject, set_current_tenant

set_current_tenant("acme")
scope_subject("inbox.message")           # -> "acme.inbox.message"
scope_subject("inbox.message", "other")  # -> "other.inbox.message"
```

These helpers run throughout the platform: [`EventBus`](events.md) scopes subjects;
[`SecretStore`](secrets.md) scopes secret paths.
