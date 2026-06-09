"""Tests for logging configuration and bound-logger output."""

from __future__ import annotations

from structlog.testing import capture_logs

from epicurus_core.config import CoreSettings
from epicurus_core.logging import configure_logging, get_logger


def test_configure_is_callable() -> None:
    configure_logging(CoreSettings(service_name="test-svc"))
    logger = get_logger("test")
    assert hasattr(logger, "info")


def test_event_and_fields_captured() -> None:
    configure_logging(CoreSettings())
    with capture_logs() as logs:
        get_logger("t").info("hello", key="value")
    assert logs[-1]["event"] == "hello"
    assert logs[-1]["key"] == "value"
    assert logs[-1]["log_level"] == "info"
