"""Tests for the log-stream ring buffer and SSE endpoint (ADR-0031)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from epicurus_core_app.log_stream import (
    _DEFAULT_LEVEL_IDX,
    _LEVELS,
    LogBuffer,
    LogEntry,
)
from epicurus_core_app.log_stream_routes import create_log_stream_router

# ---------------------------------------------------------------------------
# LogBuffer unit tests
# ---------------------------------------------------------------------------


def _emit(
    buf: LogBuffer,
    level: str = "info",
    service: str = "test",
    message: str = "hello",
) -> None:
    """Drive the processor as structlog would."""
    buf.processor(
        None,
        level,
        {
            "event": message,
            "level": level,
            "service": service,
            "timestamp": "2026-01-01T00:00:00Z",
        },
    )


def test_processor_appends_to_history() -> None:
    buf = LogBuffer()
    assert len(buf._history) == 0
    _emit(buf, message="first")
    assert len(buf._history) == 1
    entry = buf._history[0]
    assert isinstance(entry, LogEntry)
    assert entry.message == "first"
    assert entry.level == "info"
    assert entry.service == "test"


def test_processor_returns_event_dict_unchanged() -> None:
    buf = LogBuffer()
    event_dict = {"event": "hi", "level": "info", "service": "s", "timestamp": "t", "extra": 42}
    result = buf.processor(None, "info", event_dict)
    assert result is event_dict


def test_processor_redacts_secret_keys() -> None:
    buf = LogBuffer()
    buf.processor(
        None,
        "info",
        {
            "event": "login",
            "level": "info",
            "service": "auth",
            "timestamp": "t",
            "api_key": "sk-secret",
            "token": "bearer-xyz",
            "password": "hunter2",
            "credential": "abc",
            "user_key": "k",  # contains "key" -> redacted
            "auth_header": "Basic ...",  # contains "auth" -> redacted
            "username": "alice",  # safe
            "request_id": "req-1",  # safe
        },
    )
    entry = buf._history[0]
    assert "api_key" not in entry.context
    assert "token" not in entry.context
    assert "password" not in entry.context
    assert "credential" not in entry.context
    assert "user_key" not in entry.context
    assert "auth_header" not in entry.context
    assert entry.context.get("username") == "alice"
    assert entry.context.get("request_id") == "req-1"


def test_processor_ring_buffer_cap() -> None:
    buf = LogBuffer()
    for i in range(LogBuffer.MAX_HISTORY + 50):
        _emit(buf, message=f"msg-{i}")
    assert len(buf._history) == LogBuffer.MAX_HISTORY
    # The oldest entries were evicted; the newest survive.
    assert buf._history[-1].message == f"msg-{LogBuffer.MAX_HISTORY + 49}"


# ---------------------------------------------------------------------------
# stream() async generator tests
# ---------------------------------------------------------------------------


async def _collect_history(
    buf: LogBuffer,
    min_level: str | None = None,
    service_prefix: str | None = None,
) -> list[LogEntry]:
    """Collect only the history portion (no live subscription) by cancelling."""
    results: list[LogEntry] = []

    async def _drain() -> None:
        async for entry in buf.stream(min_level, service_prefix):
            results.append(entry)

    task = asyncio.create_task(_drain())
    # Give the history replay a tick to run, then cancel the live subscription.
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return results


async def test_stream_yields_history_entries() -> None:
    buf = LogBuffer()
    _emit(buf, level="info", message="a")
    _emit(buf, level="warning", message="b")
    entries = await _collect_history(buf)
    assert len(entries) == 2
    assert entries[0].message == "a"
    assert entries[1].message == "b"


async def test_stream_level_filter_excludes_below_minimum() -> None:
    buf = LogBuffer()
    _emit(buf, level="debug", message="debug-msg")
    _emit(buf, level="info", message="info-msg")
    _emit(buf, level="warning", message="warning-msg")
    _emit(buf, level="error", message="error-msg")
    entries = await _collect_history(buf, min_level="warning")
    levels = {e.level for e in entries}
    assert "debug" not in levels
    assert "info" not in levels
    assert "warning" in levels
    assert "error" in levels


async def test_stream_level_filter_debug_includes_all() -> None:
    buf = LogBuffer()
    _emit(buf, level="debug", message="d")
    _emit(buf, level="info", message="i")
    _emit(buf, level="critical", message="c")
    entries = await _collect_history(buf, min_level="debug")
    assert len(entries) == 3


async def test_stream_service_prefix_filter() -> None:
    buf = LogBuffer()
    _emit(buf, service="epicurus_core_app.agent", message="agent")
    _emit(buf, service="epicurus_core_app.llm", message="llm")
    _emit(buf, service="epicurus_core_app.agent.routes", message="agent-routes")
    entries = await _collect_history(buf, service_prefix="epicurus_core_app.agent")
    messages = [e.message for e in entries]
    assert "agent" in messages
    assert "agent-routes" in messages
    assert "llm" not in messages


async def test_stream_empty_buffer_yields_nothing_then_waits() -> None:
    entries = await _collect_history(LogBuffer())
    assert entries == []


async def test_stream_unknown_level_treated_as_info() -> None:
    buf = LogBuffer()
    # Inject an entry with a non-standard level directly.
    buf._history.append(LogEntry(ts="t", level="notice", service="s", message="m", context={}))
    # Requesting "info" min should include "notice" (treated as info-level = 1).
    entries = await _collect_history(buf, min_level="info")
    assert len(entries) == 1


async def test_stream_live_delivery() -> None:
    buf = LogBuffer()
    received: list[LogEntry] = []

    async def _consumer() -> None:
        async for entry in buf.stream("info", None):
            received.append(entry)
            if len(received) == 2:
                return

    task = asyncio.create_task(_consumer())
    await asyncio.sleep(0)  # let consumer subscribe
    _emit(buf, message="live-1")
    _emit(buf, message="live-2")
    await asyncio.wait_for(task, timeout=5.0)
    assert [e.message for e in received] == ["live-1", "live-2"]


# ---------------------------------------------------------------------------
# SSE endpoint tests
# ---------------------------------------------------------------------------
# The log stream is infinite — the generator blocks in asyncio.wait_for(q.get())
# between history and live entries.  We test the route in two ways:
#
#   1. Header / format contract: parse the first few bytes directly from the
#      generator output (unit-testing the ASGI event sequence) via
#      starlette.testclient with a generator that stops after the history replay.
#
#   2. Filtering contract: already exercised by the LogBuffer.stream() tests
#      above; those are the source-of-truth for level/service filtering.
#
# To avoid the TestClient blocking forever we use a modified LogBuffer that
# raises StopAsyncIteration after the history flush — simulating a cleanly
# completed stream so the test can read the SSE bytes synchronously.


class _FiniteLogBuffer(LogBuffer):
    """LogBuffer variant whose stream() ends after the history replay."""

    async def stream(
        self,
        min_level: str | None,
        service_prefix: str | None,
    ) -> Any:
        if min_level and min_level in _LEVELS:
            min_idx = _LEVELS.index(min_level)
        else:
            min_idx = _DEFAULT_LEVEL_IDX

        def _matches(entry: LogEntry) -> bool:
            try:
                lvl_idx = _LEVELS.index(entry.level)
            except ValueError:
                lvl_idx = 1
            return lvl_idx >= min_idx and (
                not service_prefix or entry.service.startswith(service_prefix)
            )

        for entry in list(self._history):
            if _matches(entry):
                yield entry
        # End here — no live subscription.


def _make_app() -> tuple[FastAPI, _FiniteLogBuffer]:
    buf = _FiniteLogBuffer()
    app = FastAPI()
    app.include_router(create_log_stream_router(buf))
    return app, buf


def test_sse_endpoint_returns_200_event_stream() -> None:
    app, buf = _make_app()
    _emit(buf, message="boot-msg")
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/platform/v1/logs/stream")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "event: log" in resp.text


def test_sse_endpoint_level_query_param() -> None:
    app, buf = _make_app()
    _emit(buf, level="debug", message="debug-only")
    _emit(buf, level="info", message="info-msg")
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/platform/v1/logs/stream?level=info")
    assert resp.status_code == 200
    assert "debug-only" not in resp.text
    assert "info-msg" in resp.text


def test_sse_endpoint_service_query_param() -> None:
    app, buf = _make_app()
    _emit(buf, service="a.b", message="from-ab")
    _emit(buf, service="x.y", message="from-xy")
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/platform/v1/logs/stream?service=a")
    assert resp.status_code == 200
    assert "from-ab" in resp.text
    assert "from-xy" not in resp.text


def test_sse_frame_is_valid_json() -> None:
    app, buf = _make_app()
    _emit(buf, message="json-check")
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/platform/v1/logs/stream")
    assert resp.status_code == 200
    data_line = next((line for line in resp.text.splitlines() if line.startswith("data:")), "")
    assert data_line != ""
    parsed = json.loads(data_line[len("data:") :].strip())
    assert parsed["message"] == "json-check"
    assert "ts" in parsed
    assert "level" in parsed
    assert "service" in parsed
    assert "context" in parsed
