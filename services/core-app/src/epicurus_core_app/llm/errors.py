"""Typed failures raised by the LLM gateway (ADR-0140).

These carry enough for a surface to render an actionable message: what was asked
of the model, which capability it lacks, and the one action that fixes it. They
never carry provider payloads, account ids or tokens.
"""

from __future__ import annotations


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
