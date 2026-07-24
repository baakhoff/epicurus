"""Where a run's output goes — the sink seam (ADR-0105).

The four sinks an automation can deliver to (``push``, ``chat``, ``notes``, ``kb``) are
each a companion issue: the push send API, the chat/notes/kb delivery. None of them exist
to call yet, and this issue must not wait for them or invent them.

So this is a **seam, not an implementation**. A sink is a named async callable registered
here; :class:`SinkDispatcher` fans out to whichever are registered and reports what fired.
An unregistered sink is *not* an error — it is a sink whose issue has not landed — and the
run is still complete, because **the ledger always records the output** whether anything
delivered it or not (:meth:`AutomationStore.record_run`). That is what makes this
degradation graceful rather than lossy: nothing is silently dropped, it is written down and
simply not announced yet.

The rule the fan-out exists to keep: **it happens after the turn, deterministically.** The
model does not choose whether to notify anyone — it produces an answer, and the sinks the
operator configured receive it. A model that could decide its own reporting could decide
not to report.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from epicurus_core import EntityRef, get_logger
from epicurus_core_app.automations.model import Automation, Sink

log = get_logger("epicurus_core_app.automations.sinks")

#: Deliver one run's output, optionally returning an ``EntityRef`` for an artifact it produced —
#: a notes/kb document (#672). ``(automation, output) -> EntityRef | None``; raising means "did
#: not deliver", which the dispatcher records rather than propagates.
SinkHandler = Callable[[Automation, str], Awaitable["EntityRef | None"]]


@dataclass
class SinkResult:
    """What the fan-out managed to do — recorded on the ledger entry."""

    fired: list[str] = field(default_factory=list)
    #: Configured but not registered yet (its companion issue hasn't landed).
    unavailable: list[str] = field(default_factory=list)
    #: Registered, attempted, and raised. The run itself is unaffected.
    failed: list[str] = field(default_factory=list)
    #: EntityRefs for documents the sinks produced (#672) — so the runs feed links what was made.
    artifacts: list[EntityRef] = field(default_factory=list)


class SinkDispatcher:
    """Fans a finished run's output out to the sinks its automation configured.

    Handlers are registered by the wiring that owns them, so this module never imports
    push, chat, notes, or kb — it stays the seam and they stay their own issues.
    """

    def __init__(self) -> None:
        self._handlers: dict[Sink, SinkHandler] = {}

    def register(self, sink: Sink, handler: SinkHandler) -> None:
        """Wire the handler for one sink. Registering twice replaces."""
        self._handlers[sink] = handler

    def registered(self) -> set[Sink]:
        """Which sinks can currently deliver — for diagnostics and tests."""
        return set(self._handlers)

    async def dispatch(self, automation: Automation, output: str) -> SinkResult:
        """Deliver *output* to every sink *automation* configured.

        Never raises. A sink that fails is recorded and the rest still run: one broken
        delivery must not cost the others, and none of them can undo the run that already
        happened.

        Silent-act delivers nowhere by design — the caller checks
        :meth:`Automation.fires_sinks` before calling, and this asserts nothing about it.

        The ``chat`` sink is **skipped here**: it is realized at turn time by the runner (the run
        persists into the session so a rolling chat is reply-able), not as a post-run fan-out. The
        runner records ``chat`` as fired itself.
        """
        result = SinkResult()
        for sink in automation.sinks:
            if sink == "chat":
                continue  # turn-time, handled by the runner — see the docstring
            handler = self._handlers.get(sink)
            if handler is None:
                # Not an error: the sink's issue hasn't landed. The output is on the
                # ledger regardless, so nothing is lost — only unannounced.
                result.unavailable.append(sink)
                log.debug(
                    "sink not registered; output recorded on the ledger only",
                    sink=sink,
                    automation=automation.id,
                )
                continue
            try:
                artifact = await handler(automation, output)
                result.fired.append(sink)
                if artifact is not None:
                    result.artifacts.append(artifact)
            except Exception as exc:  # one sink's failure must not cost the others
                result.failed.append(sink)
                log.warning(
                    "sink delivery failed",
                    sink=sink,
                    automation=automation.id,
                    tenant=automation.tenant,
                    error=str(exc),
                )
        return result


__all__ = ["SinkDispatcher", "SinkHandler", "SinkResult"]
