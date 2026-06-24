"""Separate a model's chain-of-thought from its answer in a content stream.

Local reasoning models (deepseek-r1, qwen3, …) served via Ollama inline their thinking in
``<think>…</think>`` spans inside the normal content stream, rather than in a separate
``reasoning_content`` field the way hosted reasoning APIs do. :class:`ThinkSplitter` routes
those spans to a *thinking* channel and the rest to the *answer* channel, tolerating a tag
split across chunk boundaries, so the agent can surface thinking in the activity timeline
(ADR-0041) while keeping the answer itself clean.
"""

from __future__ import annotations

OPEN_TAG = "<think>"
CLOSE_TAG = "</think>"


def _held_partial_len(text: str, tag: str) -> int:
    """Length of the longest suffix of ``text`` that is a *proper* prefix of ``tag``.

    Such a tail might be the start of a tag that the next chunk completes, so the caller
    holds it back rather than emitting it into the wrong channel. Returns 0 when no suffix
    of ``text`` begins a tag.
    """
    for n in range(min(len(text), len(tag) - 1), 0, -1):
        if text.endswith(tag[:n]):
            return n
    return 0


class ThinkSplitter:
    """Stateful splitter of a streamed content into ``(answer, thinking)`` by think spans."""

    def __init__(self) -> None:
        self._in_think = False
        self._held = ""  # a buffered tail that may be the start of the next tag

    def feed(self, piece: str) -> tuple[str, str]:
        """Consume one content delta; return its ``(answer, thinking)`` parts.

        Either part may be empty. A trailing fragment that could begin a tag is held back
        and prepended to the next ``feed`` (or released by :meth:`flush`).
        """
        answer: list[str] = []
        thinking: list[str] = []
        buf = self._held + piece
        self._held = ""
        i = 0
        while i < len(buf):
            tag = CLOSE_TAG if self._in_think else OPEN_TAG
            idx = buf.find(tag, i)
            if idx == -1:
                rest = buf[i:]
                hold = _held_partial_len(rest, tag)
                emit = rest[: len(rest) - hold] if hold else rest
                (thinking if self._in_think else answer).append(emit)
                self._held = rest[len(rest) - hold :] if hold else ""
                break
            (thinking if self._in_think else answer).append(buf[i:idx])
            self._in_think = not self._in_think
            i = idx + len(tag)
        return "".join(answer), "".join(thinking)

    def flush(self) -> tuple[str, str]:
        """At stream end, release any held tail as literal text in the current channel."""
        tail, self._held = self._held, ""
        if not tail:
            return "", ""
        return ("", tail) if self._in_think else (tail, "")


def split_reasoning(content: str) -> tuple[str, str]:
    """Split a complete (non-streamed) message into ``(answer, thinking)``."""
    splitter = ThinkSplitter()
    answer, thinking = splitter.feed(content)
    answer_tail, thinking_tail = splitter.flush()
    return answer + answer_tail, thinking + thinking_tail
