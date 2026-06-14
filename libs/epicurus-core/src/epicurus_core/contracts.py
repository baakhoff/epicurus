"""The shared chat contract — the single source of truth for chat message and
result shapes used across the platform (ADR-0021).

The core's LLM gateway, its module-facing platform API, and the typed
``PlatformClient`` that modules call all speak the same two shapes:

* :class:`ChatMessage` — one message in a chat exchange (request side).
* :class:`ChatResult` — a non-streaming chat completion (response side).

Both live here so there is exactly one definition; ``epicurus_core_app`` imports
them rather than redefining its own. ``PlatformMessage`` and
``PlatformChatResponse`` are retained as backward-compatible aliases so existing
module code (``from epicurus_core import PlatformMessage``) keeps working.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

Role = Literal["system", "user", "assistant", "tool"]


class ChatMessage(BaseModel):
    """One message in a chat exchange.

    ``content`` is optional: an assistant tool-call turn carries ``tool_calls`` with no
    content, and a ``tool`` result carries ``tool_call_id`` + ``name``.
    """

    role: Role
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ChatResult(BaseModel):
    """A non-streaming chat completion."""

    model: str
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


# ── Backward-compatible aliases ──────────────────────────────────────────────────
# Modules historically imported these names from ``epicurus_core``. They are the
# same shapes as the canonical contract above — kept as aliases, not duplicate
# definitions, so there is still one source of truth.
PlatformMessage = ChatMessage
PlatformChatResponse = ChatResult
