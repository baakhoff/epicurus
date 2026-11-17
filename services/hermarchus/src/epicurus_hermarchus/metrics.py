"""Prometheus metrics, on a registry of the app's own (so tests can build many apps).

Labels are bounded: event types and decisions come from fixed vocabularies in this package;
no session id, title, path or prompt ever becomes a label.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge

__all__ = ["Metrics"]


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.sessions_live = Gauge(
            "hermarchus_sessions_live",
            "Sessions with a live agent process (starting, idle or running).",
            registry=self.registry,
        )
        self.sessions_created = Counter(
            "hermarchus_sessions_created",
            "Sessions created, by agent kind.",
            ["agent"],
            registry=self.registry,
        )
        self.events = Counter(
            "hermarchus_events",
            "Session events recorded, by type.",
            ["type"],
            registry=self.registry,
        )
        self.stream_overflows = Counter(
            "hermarchus_stream_overflows",
            "Streaming connections dropped because they fell behind.",
            registry=self.registry,
        )
        self.permission_resolutions = Counter(
            "hermarchus_permission_resolutions",
            "Permission requests resolved, by decision and by whom.",
            ["decision", "by"],
            registry=self.registry,
        )
