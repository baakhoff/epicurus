"""epicurus-core — shared contract and runtime for epicurus services.

Cross-service building blocks: configuration, structured logging, the tenant
scoping primitive, the NATS event backbone, the MCP module contract, and the
operational ``/health`` + ``/metrics`` surface. The OpenBao client lands next.
"""

from __future__ import annotations

from epicurus_core._version import __version__
from epicurus_core.config import CoreSettings, Environment, LogLevel
from epicurus_core.events import Event, EventBus, EventHandler, Payload, Replier
from epicurus_core.logging import configure_logging, get_logger
from epicurus_core.manifest import CONTRACT_VERSION, EventSpec, ModuleManifest, ToolSpec
from epicurus_core.module import EpicurusModule
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
    "CONTRACT_VERSION",
    "CoreSettings",
    "Environment",
    "EpicurusModule",
    "Event",
    "EventBus",
    "EventHandler",
    "EventSpec",
    "HealthResponse",
    "LogLevel",
    "ModuleManifest",
    "Payload",
    "Replier",
    "TenantError",
    "ToolSpec",
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
