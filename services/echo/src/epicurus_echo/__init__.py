"""Echo module — the reference module that proves the epicurus contract.

It exercises both halves of the module↔core contract: an agent-facing MCP tool
(`echo`) and the NATS event path (request/reply on `echo.request`).
"""

from __future__ import annotations

from epicurus_echo.service import ECHO_SUBJECT, build_module, echo_responder, serve_responder

__all__ = ["ECHO_SUBJECT", "build_module", "echo_responder", "serve_responder"]
