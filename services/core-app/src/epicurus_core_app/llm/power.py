"""Power-state control for the local LLM runtime (ADR-0005)."""

from __future__ import annotations

from epicurus_core_app.llm.models import PowerState


class GatewayPausedError(RuntimeError):
    """Raised when inference is requested while the runtime is paused."""


class PowerController:
    """Holds the runtime power state.

    ``Paused`` is operator-controlled and refuses GPU work (the gateway also asks the
    runtime to unload models). ``Active`` / ``Idle`` are a coarse signal — actual model
    unloading is driven by Ollama's ``keep_alive`` (ADR-0005).
    """

    def __init__(self) -> None:
        self._state = PowerState.IDLE

    @property
    def state(self) -> PowerState:
        return self._state

    @property
    def paused(self) -> bool:
        return self._state is PowerState.PAUSED

    def mark_active(self) -> None:
        """Note that a model is being used (no-op while paused)."""
        if self._state is not PowerState.PAUSED:
            self._state = PowerState.ACTIVE

    def pause(self) -> None:
        self._state = PowerState.PAUSED

    def resume(self) -> None:
        self._state = PowerState.IDLE
