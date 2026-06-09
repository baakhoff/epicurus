"""epicurus-core — shared contract and runtime for epicurus services.

This package will hold the cross-service building blocks (config, structured
logging, NATS client, MCP base classes, OpenBao client, tenant context, and the
``/health`` + ``/metrics`` surface). This is the scaffolding release; those
modules land in follow-up changes.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.0.1"
