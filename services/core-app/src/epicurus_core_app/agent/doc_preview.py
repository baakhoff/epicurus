"""The document typewriter: a write, watched as the model types it (#654, ADR-0121).

v1 of the document pane (#541, ADR-0101) opens the pane when an annotated tool call *lands* — by
then the model has already written the whole body. v2 shows it arriving. The gateway now surfaces
tool-call fragments as they stream (``StreamEvent.tool_call``); this module turns that firehose
into a small number of ``doc_preview`` frames:

* **Only annotated calls.** A fragment's tool name is resolved once per call against the module
  registry's ``writes_document`` annotation (ADR-0100). Anything else is ignored from then on, at
  the cost of one dict lookup per fragment.
* **Only the body.** :class:`~epicurus_core_app.agent.partial_json.StreamingArguments` decodes the
  annotated ``content_arg`` out of the still-unterminated JSON; ``target_arg`` / ``title_arg`` are
  reported only once *complete*, so the pane's header never flickers through a half-typed title.
* **Coalesced.** A frame is emitted at most every :data:`PREVIEW_INTERVAL_S`, or sooner if
  :data:`PREVIEW_MAX_CHARS` have piled up. The first chunk goes out immediately (the pane should
  appear at once) and :meth:`DocumentPreviewTracker.flush` releases the tail, so nothing is ever
  withheld at the end. This is the whole answer to #541's "a large document must never starve the
  chat deltas": the two share one stream, so what protects the deltas is bounding how many frames
  the document may put on it — 10/s, whatever the model's token rate.

Frames are **ephemeral by construction**: the tracker returns data, the loop turns it into an
``AgentEvent``, and nothing here ever touches the turn's timeline. ADR-0041's caps are unchanged —
the same reason v1's ``document`` payload is deliberately absent from ``append_tool``.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from epicurus_core import WritesDocument, get_logger
from epicurus_core_app.agent.partial_json import StreamingArguments
from epicurus_core_app.llm.models import ToolCallFragment

log = get_logger("epicurus_core_app.agent.doc_preview")

__all__ = [
    "PREVIEW_INTERVAL_S",
    "PREVIEW_MAX_CHARS",
    "DocumentPreviewTracker",
    "DocumentToolLookup",
    "PreviewFrame",
]

#: Coalesce window: at most one preview frame per call per this many seconds. 100 ms is under the
#: threshold at which text stops reading as "being typed", and it bounds the document's share of
#: the SSE stream at ~10 frames/second no matter how fast the model emits tokens.
PREVIEW_INTERVAL_S = 0.1
#: …but never withhold more than this much text: a model that dumps a whole document inside one
#: window would otherwise produce a single enormous frame. Bounds frame *size* the way the
#: interval bounds frame *rate*.
PREVIEW_MAX_CHARS = 4096

#: Resolves a tool name to ``(module_name, annotation)`` when the tool declares
#: ``writes_document``, else ``None`` (#541, ADR-0100). Backed by the module registry's
#: manifests; injected so the agent loop needn't know the registry exists.
DocumentToolLookup = Callable[[str], Awaitable[tuple[str, WritesDocument] | None]]


@dataclass(frozen=True)
class PreviewFrame:
    """One coalesced slice of a document being written.

    ``text`` is the *delta* — the characters decoded since the previous frame — so the sequence
    of frames concatenates to the body. ``target`` / ``title`` are present only once their
    argument has fully arrived, and repeat on every later frame so each frame identifies its own
    document (a client re-attaching mid-write needs no earlier frame to make sense of this one).
    """

    tool: str
    module: str
    text: str
    target: str | None = None
    title: str | None = None


@dataclass
class _Call:
    """Per-tool-call state: what it writes, and how much of it has gone out."""

    tool: str
    module: str
    spec: WritesDocument
    reader: StreamingArguments
    last_emit: float | None = None


@dataclass
class _Unresolved:
    """Argument text that arrived before the call's name did, held for replay.

    Providers name a call on its first fragment in practice; this makes the tracker independent
    of that ordering rather than dependent on it.
    """

    parts: list[str] = field(default_factory=list)


class DocumentPreviewTracker:
    """Turns a gateway call's tool-call fragments into coalesced document previews.

    One instance per ``stream_chat`` call: slot numbers are only unique within a stream, and a
    fresh instance is how the tracker forgets the previous step's calls. Feeding it is
    best-effort throughout — a registry hiccup or malformed JSON costs a preview, never the turn.
    """

    def __init__(
        self,
        lookup: DocumentToolLookup | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        interval_s: float = PREVIEW_INTERVAL_S,
        max_chars: int = PREVIEW_MAX_CHARS,
    ) -> None:
        self._lookup = lookup
        self._clock = clock
        self._interval = interval_s
        self._max_chars = max_chars
        # slot → the call's preview state, or None for "resolved, and not a document write".
        self._slots: dict[int, _Call | None] = {}
        self._early: dict[int, _Unresolved] = {}

    async def feed(self, fragment: ToolCallFragment) -> PreviewFrame | None:
        """Take one streamed fragment; return a frame when the throttle says it is time."""
        if self._lookup is None:
            return None
        call = await self._resolve(fragment)
        if call is None:
            return None
        if fragment.arguments:
            call.reader.feed(fragment.arguments)
        return self._emit(call, forced=False)

    def flush(self) -> list[PreviewFrame]:
        """Release every call's withheld tail — the gateway stream is over.

        Without this the last coalesce window's characters would sit in the tracker until a next
        fragment that never comes, and the pane would stop a few words short of the document.
        """
        frames = [
            frame
            for call in self._slots.values()
            if call is not None and (frame := self._emit(call, forced=True)) is not None
        ]
        return frames

    async def _resolve(self, fragment: ToolCallFragment) -> _Call | None:
        """The call's preview state, resolving its annotation the first time it is named."""
        lookup = self._lookup
        if lookup is None:
            return None
        if fragment.slot in self._slots:  # already answered — including "not a document write"
            return self._slots[fragment.slot]
        if not fragment.name:
            # Not named yet: hold any argument text so nothing is lost if the name comes later.
            if fragment.arguments:
                self._early.setdefault(fragment.slot, _Unresolved()).parts.append(
                    fragment.arguments
                )
            return None
        found = None
        try:
            found = await lookup(fragment.name)
        except Exception as exc:  # a registry hiccup must not take the turn down with it
            log.debug("document annotation lookup failed", tool=fragment.name, error=str(exc))
        if found is None:
            self._slots[fragment.slot] = None
            self._early.pop(fragment.slot, None)
            return None
        module, spec = found
        keys = [spec.content_arg, *(a for a in (spec.target_arg, spec.title_arg) if a)]
        call = _Call(tool=fragment.name, module=module, spec=spec, reader=StreamingArguments(*keys))
        for part in self._early.pop(fragment.slot, _Unresolved()).parts:
            call.reader.feed(part)
        self._slots[fragment.slot] = call
        return call

    def _emit(self, call: _Call, *, forced: bool) -> PreviewFrame | None:
        """A frame for *call*, if it has text to send and the throttle allows it."""
        pending = call.reader.pending(call.spec.content_arg)
        if pending <= 0:
            return None  # a pane with nothing in it is worse than no pane (v1's rule)
        now = self._clock()
        if not forced and call.last_emit is not None:
            within_window = (now - call.last_emit) < self._interval
            if within_window and pending < self._max_chars:
                return None
        call.last_emit = now
        return PreviewFrame(
            tool=call.tool,
            module=call.module,
            text=call.reader.drain(call.spec.content_arg),
            target=self._settled(call, call.spec.target_arg),
            title=self._settled(call, call.spec.title_arg),
        )

    def _settled(self, call: _Call, arg: str | None) -> str | None:
        """A companion argument's value — only once it is *complete*, else ``None``.

        The body is shown half-written on purpose; a title is not. Reporting a partially typed
        one would have the pane's header rewrite itself character by character, and reporting a
        truncated one would be a lie about what the document is called.
        """
        if not arg or not call.reader.closed(arg):
            return None
        return call.reader.text(arg) or None
