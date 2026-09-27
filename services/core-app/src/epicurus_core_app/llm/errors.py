"""Typed failures raised by the LLM gateway (ADR-0140).

These carry enough for a surface to render an actionable message: what was asked
of the model, which capability it lacks, and the one action that fixes it. They
never carry provider payloads, account ids or tokens.
"""

from __future__ import annotations

from typing import Literal

LocalRuntimeState = Literal["absent", "unreachable", "ok"]
"""What the deployment's local LLM runtime is doing (#962, ADR-0144).

``absent`` — none is configured (``OLLAMA_URL`` blank), deliberately. ``unreachable`` — one is
configured and did not answer. ``ok`` — it answered. Every local-runtime surface branches on
exactly these three; nothing anywhere may re-derive them from a caught exception.
"""


class ModelCapabilityError(RuntimeError):
    """The selected model cannot serve this request (#944, #947).

    Args:
        model: The model id the request resolved to.
        capability: ``"chat"``, ``"tools"`` or ``"embedding"`` — the capability
            the model is missing for this request.
        message: Operator-readable explanation. No provider ids, no raw payloads.
        hint: The single action that resolves it, e.g. "Pick a chat model on the
            Models page".
    """

    def __init__(self, *, model: str, capability: str, message: str, hint: str) -> None:
        super().__init__(message)
        self.model = model
        self.capability = capability
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return self.message


class LocalRuntimeUnavailableError(RuntimeError):
    """A local-runtime-only action was asked of a deployment that cannot serve it (#962).

    Carries which of the two non-serving states applies, because they are different facts
    with different answers, and collapsing them is what made every one of these paths a bare
    500 (ADR-0144):

    * ``absent`` — no local runtime is configured (``OLLAMA_URL`` is blank). A deliberate
      hosted-only deployment. Nothing is wrong; the action simply does not exist here, so the
      surface answers **409**.
    * ``unreachable`` — a runtime *is* configured and did not answer. That is an error, and
      the surface answers **502**.

    Args:
        state: ``"absent"`` or ``"unreachable"``.
        message: Operator-readable explanation naming the mode. No payloads, no URLs with
            credentials in them.
    """

    def __init__(self, *, state: LocalRuntimeState, message: str) -> None:
        super().__init__(message)
        self.state = state
        self.message = message

    def __str__(self) -> str:
        return self.message
