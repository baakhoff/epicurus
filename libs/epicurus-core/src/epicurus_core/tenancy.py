"""Tenant scoping — the dual-track primitive.

Every persisted or addressable resource is namespaced by tenant, even when a
single tenant is running, so the *same* code serves self-host (one tenant) and
multi-tenant SaaS. See docs/DUAL-TRACK.md. This module is dependency-free on
purpose: it is the lowest layer and everything else may import it.
"""

from __future__ import annotations

import re
from contextvars import ContextVar, Token

__all__ = [
    "TenantError",
    "current_tenant",
    "is_valid_tenant_id",
    "reset_current_tenant",
    "scope_bucket",
    "scope_collection",
    "scope_secret_path",
    "scope_subject",
    "set_current_tenant",
    "validate_tenant_id",
]

# Lowercase alphanumerics with single internal hyphens; 1-63 chars. Deliberately
# strict so one id is safe across NATS subjects, Qdrant collection names,
# object-store buckets, and OpenBao secret paths without escaping.
_TENANT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

_current_tenant: ContextVar[str | None] = ContextVar("epicurus_current_tenant", default=None)


class TenantError(RuntimeError):
    """Raised when a tenant id is missing or invalid where one is required."""


def is_valid_tenant_id(tenant_id: str) -> bool:
    """Return whether ``tenant_id`` is a well-formed tenant identifier."""
    return _TENANT_RE.fullmatch(tenant_id) is not None


def validate_tenant_id(tenant_id: str) -> str:
    """Return ``tenant_id`` if valid, else raise :class:`TenantError`."""
    if not is_valid_tenant_id(tenant_id):
        raise TenantError(
            f"invalid tenant id {tenant_id!r}: must be 1-63 chars, lowercase "
            "alphanumeric and hyphens, with no leading or trailing hyphen"
        )
    return tenant_id


def set_current_tenant(tenant_id: str) -> Token[str | None]:
    """Bind the current tenant for this context; returns a token for reset."""
    return _current_tenant.set(validate_tenant_id(tenant_id))


def reset_current_tenant(token: Token[str | None]) -> None:
    """Undo a previous :func:`set_current_tenant`, restoring the prior tenant."""
    _current_tenant.reset(token)


def current_tenant() -> str:
    """Return the tenant bound to this context, or raise if none is set."""
    tenant = _current_tenant.get()
    if tenant is None:
        raise TenantError("no current tenant is set in this context")
    return tenant


def _resolve(tenant_id: str | None) -> str:
    return validate_tenant_id(tenant_id) if tenant_id is not None else current_tenant()


def scope_subject(base: str, tenant_id: str | None = None) -> str:
    """NATS subject namespaced by tenant: ``<tenant>.<base>``."""
    return f"{_resolve(tenant_id)}.{base}"


def scope_collection(base: str, tenant_id: str | None = None) -> str:
    """Qdrant collection namespaced by tenant: ``<tenant>__<base>``."""
    return f"{_resolve(tenant_id)}__{base}"


def scope_secret_path(base: str, tenant_id: str | None = None) -> str:
    """OpenBao secret path namespaced by tenant: ``tenants/<tenant>/<base>``."""
    return f"tenants/{_resolve(tenant_id)}/{base.lstrip('/')}"


def scope_bucket(base: str, tenant_id: str | None = None) -> str:
    """Object-store bucket namespaced by tenant: ``<tenant>-<base>``."""
    return f"{_resolve(tenant_id)}-{base}"
