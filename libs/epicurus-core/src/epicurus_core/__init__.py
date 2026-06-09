"""epicurus-core — shared contract and runtime for epicurus services.

Cross-service building blocks: configuration, structured logging, the tenant
scoping primitive, the NATS event backbone, and the operational ``/health`` +
``/metrics`` surface. The MCP base classes and OpenBao client land in follow-ups.
"""

from __future__ import annotations

from epicurus_core._version import __version__
from epicurus_core.config import CoreSettings, Environment, LogLevel
from epicurus_core.events import Event, EventBus, EventHandler, Payload, Replier
from epicurus_core.logging import configure_logging, get_logger
from epicurus_core.observability import HealthResponse, add_ops_routes, create_ops_router
from epicurus_core.tenancy import (
    TenantError,
    current_tenant,
    is_valid_tenant_id,
    reset_current_tenant,
    scope_bucket,
    scope_collection,
    scope_secret_path,
    scope_subject,
    set_current_tenant,
    validate_tenant_id,
)

__all__ = [
    "CoreSettings",
    "Environment",
    "Event",
    "EventBus",
    "EventHandler",
    "HealthResponse",
    "LogLevel",
    "Payload",
    "Replier",
    "TenantError",
    "__version__",
    "add_ops_routes",
    "configure_logging",
    "create_ops_router",
    "current_tenant",
    "get_logger",
    "is_valid_tenant_id",
    "reset_current_tenant",
    "scope_bucket",
    "scope_collection",
    "scope_secret_path",
    "scope_subject",
    "set_current_tenant",
    "validate_tenant_id",
]
