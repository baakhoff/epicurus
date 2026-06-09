"""Structured logging via structlog, configured consistently across services.

Console-rendered in local dev, JSON in staging/production (overridable). Tenant
and request context are carried via ``structlog.contextvars`` so every line in a
request is correlated without threading a logger through call stacks.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, cast

import structlog

from epicurus_core.config import CoreSettings

__all__ = ["configure_logging", "get_logger"]


def configure_logging(settings: CoreSettings) -> None:
    """Configure structlog for the process. Safe to call once at startup."""
    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)

    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if settings.use_json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Stamp every line with the service name.
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(service=settings.service_name)


def get_logger(*name: str, **initial_values: Any) -> structlog.typing.FilteringBoundLogger:
    """Return a bound logger. Call :func:`configure_logging` once first."""
    return cast(
        "structlog.typing.FilteringBoundLogger",
        structlog.get_logger(*name, **initial_values),
    )
