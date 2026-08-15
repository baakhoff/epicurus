"""Read named string arguments out of *syntactically incomplete* tool-call JSON (#654).

A model streams a tool call's arguments as JSON text, a few characters at a time. To show a
document being written (ADR-0121) the loop must read one named argument's value *while the JSON
is still an unterminated fragment* — ``{"path": "a.md", "content": "# Goa`` — which ``json.loads``
can only reject. This module is that reader.

It is a hand-rolled, resumable scanner rather than a tolerant-parser dependency, because the job
is narrower than "parse partial JSON" and the failure modes are exactly the ones a general parser
does not solve for us:

* **Only the value matters, decoded and incremental.** A tolerant parser rebuilds the whole
  object per fragment (O(n²) over a long document) and hands back a value that was re-decoded
  from the start; what a typewriter needs is *the characters added since last time*.
* **A fragment boundary can split anything** — a ``\\`` from the character it escapes, a
  ``\\uXXXX`` escape from its hex digits, a surrogate pair from its other half. The scanner holds
  those back in its own state and emits only decoded, complete characters, so a consumer can
  never observe a half-escape or a lone surrogate (which would not survive JSON re-encoding on
  the way out to the browser).
* **Nothing here may throw.** Malformed JSON costs the preview, never the turn: the scanner marks
  itself :attr:`~StreamingArguments.broken` and stops, keeping whatever it had already decoded.

Only *top-level* keys are matched, by a real scanner rather than a regex — ``{"title": "content",
"content": "real"}`` must not confuse the two, and a nested object's ``content`` is not this
call's document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto

__all__ = ["StreamingArguments"]

_WHITESPACE = frozenset(" \t\r\n")
_HEX = frozenset("0123456789abcdefABCDEF")
_SIMPLE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
#: What an unpaired surrogate decodes to. A lone ``\\ud83d`` is a legal JSON escape but not a legal
#: character: Python keeps it in a ``str`` and then *fails to encode it* on the way out (the SSE
#: frame is JSON), so a preview carrying one would kill the stream it rides. Substituting here
#: keeps a malformed model output cosmetic.
_REPLACEMENT = "�"


class _State(Enum):
    SEEK_OBJECT = auto()  # before the opening `{`
    SEEK_KEY = auto()  # between members: whitespace, `,`, `"` (a key) or `}`
    IN_KEY = auto()  # inside a key string
    SEEK_COLON = auto()
    SEEK_VALUE = auto()
    IN_STRING = auto()  # inside a string *value*
    IN_NESTED = auto()  # inside an object/array value, skipping it
    IN_SCALAR = auto()  # inside a number / true / false / null
    DONE = auto()  # past the closing `}`


class _Escape(Enum):
    NONE = auto()
    BACKSLASH = auto()  # a `\` was seen; the next character says what it escapes
    UNICODE = auto()  # a `\u` was seen; collecting up to four hex digits


@dataclass
class _Value:
    """One tracked argument's decoded value, as an append-only run of chunks.

    Kept as chunks rather than one string so :meth:`StreamingArguments.drain` costs the size of
    the *delta*, not of the document — the throttle drains on every tick, and a re-join per tick
    would make a long write quadratic.
    """

    parts: list[str] = field(default_factory=list)
    cursor: int = 0  # index of the first part not yet drained
    length: int = 0  # decoded characters seen
    drained: int = 0  # decoded characters handed out
    closed: bool = False  # the closing quote arrived — the value is final

    def append(self, text: str) -> None:
        self.parts.append(text)
        self.length += len(text)


class StreamingArguments:
    """Decodes named top-level string arguments from a tool call's streaming JSON.

    Feed it argument text as it arrives (:meth:`feed`); ask it what it has (:meth:`text`,
    :meth:`drain`, :meth:`pending`, :meth:`closed`). Untracked members are scanned past without
    being accumulated, so memory is bounded by the tracked values themselves.

    A tracked key that appears **twice** keeps the first occurrence: the value it hands out is
    append-only (a consumer has already been given the earlier characters), so a duplicate key in
    a malformed stream must not rewind it.
    """

    def __init__(self, *keys: str) -> None:
        self._values: dict[str, _Value] = {key: _Value() for key in keys}
        self._state = _State.SEEK_OBJECT
        self._key_parts: list[str] = []
        self._target: _Value | None = None  # the tracked value the current string feeds, if any
        self._escape = _Escape.NONE
        self._hex = ""
        self._high: int | None = None  # a high surrogate awaiting its pair
        self._depth = 0  # nesting depth while skipping a structured value
        self._nested_string = False
        self._nested_escape = False
        self.broken = False

    # ── reading ──────────────────────────────────────────────────────────────

    def text(self, key: str) -> str:
        """Everything decoded for *key* so far (``""`` when the key hasn't been seen)."""
        value = self._values.get(key)
        return "".join(value.parts) if value is not None else ""

    def drain(self, key: str) -> str:
        """The characters decoded for *key* since the last drain, and forget them.

        Costs the size of the delta, not of the value — this is what the preview throttle
        emits on each tick.
        """
        value = self._values.get(key)
        if value is None:
            return ""
        chunk = "".join(value.parts[value.cursor :])
        value.cursor = len(value.parts)
        value.drained = value.length
        return chunk

    def pending(self, key: str) -> int:
        """How many decoded characters are waiting to be drained for *key*."""
        value = self._values.get(key)
        return 0 if value is None else value.length - value.drained

    def closed(self, key: str) -> bool:
        """Whether *key*'s string value has been terminated — i.e. the value is final."""
        value = self._values.get(key)
        return value is not None and value.closed

    # ── feeding ──────────────────────────────────────────────────────────────

    def feed(self, chunk: str) -> None:
        """Scan more argument text. Never raises: bad JSON only sets :attr:`broken`."""
        if self.broken or self._finished or not chunk:
            return
        try:
            for char in chunk:
                self._step(char)
                if self.broken or self._finished:
                    return
        except Exception:  # defensive: a preview is never worth an exception in the turn
            self.broken = True

    @property
    def _finished(self) -> bool:
        """Past the object's closing brace — there is nothing left to read."""
        return self._state is _State.DONE

    def _step(self, char: str) -> None:
        state = self._state
        if state is _State.IN_STRING:
            self._string_char(char)
        elif state is _State.IN_KEY:
            self._key_char(char)
        elif state is _State.SEEK_OBJECT:
            if char == "{":
                self._state = _State.SEEK_KEY
            elif char not in _WHITESPACE:
                self.broken = True  # not an argument object — nothing here to extract
        elif state is _State.SEEK_KEY:
            if char == '"':
                self._key_parts = []
                self._state = _State.IN_KEY
            elif char == "}":
                self._state = _State.DONE
            elif char not in _WHITESPACE and char != ",":
                self.broken = True
        elif state is _State.SEEK_COLON:
            if char == ":":
                self._state = _State.SEEK_VALUE
            elif char not in _WHITESPACE:
                self.broken = True
        elif state is _State.SEEK_VALUE:
            self._value_char(char)
        elif state is _State.IN_NESTED:
            self._nested_char(char)
        elif state is _State.IN_SCALAR:
            if char == "}":
                self._state = _State.DONE
            elif char == ",":
                self._state = _State.SEEK_KEY
            elif char not in _WHITESPACE and not (
                char.isalnum() or char in "+-.eE"
            ):  # pragma: no cover - a scalar can only hold these
                self.broken = True

    def _value_char(self, char: str) -> None:
        if char in _WHITESPACE:
            return
        key = "".join(self._key_parts)
        if char == '"':
            # A tracked key whose value already closed keeps the first occurrence (append-only).
            value = self._values.get(key)
            self._target = value if value is not None and not value.closed else None
            self._escape = _Escape.NONE
            self._high = None
            self._state = _State.IN_STRING
        elif char in "{[":
            self._depth = 1
            self._nested_string = False
            self._nested_escape = False
            self._state = _State.IN_NESTED
        elif char == "}":  # `{"a":}` — malformed, but never fatal
            self.broken = True
        else:
            self._state = _State.IN_SCALAR

    def _key_char(self, char: str) -> None:
        # Keys are plain in practice; decode them with the same escape rules anyway so a quoted
        # or escaped key can't desynchronise the scanner.
        if self._escape is _Escape.BACKSLASH:
            self._escape = _Escape.NONE
            self._key_parts.append(_SIMPLE_ESCAPES.get(char, char))
        elif char == "\\":
            self._escape = _Escape.BACKSLASH
        elif char == '"':
            self._state = _State.SEEK_COLON
        else:
            self._key_parts.append(char)

    def _string_char(self, char: str) -> None:
        """One character inside a string value — the only place decoding happens."""
        if self._escape is _Escape.UNICODE:
            if char not in _HEX:
                self._escape = _Escape.NONE
                self.broken = True
                return
            self._hex += char
            if len(self._hex) == 4:
                self._escape = _Escape.NONE
                self._code_point(int(self._hex, 16))
                self._hex = ""
            return
        if self._escape is _Escape.BACKSLASH:
            self._escape = _Escape.NONE
            if char == "u":
                # A pending high surrogate may still find its pair — don't flush it yet.
                self._escape = _Escape.UNICODE
                self._hex = ""
                return
            self._flush_high()
            self._emit(_SIMPLE_ESCAPES.get(char, char))
            return
        if char == "\\":
            self._escape = _Escape.BACKSLASH
            return
        if char == '"':
            self._flush_high()
            if self._target is not None:
                self._target.closed = True
            self._target = None
            self._state = _State.SEEK_KEY
            return
        self._flush_high()
        self._emit(char)

    def _code_point(self, code: int) -> None:
        """Emit one decoded ``\\uXXXX`` escape, pairing surrogates across fragments."""
        if 0xD800 <= code <= 0xDBFF:  # high surrogate — hold it for its pair
            self._flush_high()
            self._high = code
            return
        if 0xDC00 <= code <= 0xDFFF:  # low surrogate — completes a held pair, or is unpaired
            if self._high is not None:
                pair = 0x10000 + ((self._high - 0xD800) << 10) + (code - 0xDC00)
                self._high = None
                self._emit(chr(pair))
            else:
                self._emit(_REPLACEMENT)
            return
        self._flush_high()
        self._emit(chr(code))

    def _flush_high(self) -> None:
        """A held high surrogate that turned out to have no pair decodes to U+FFFD."""
        if self._high is not None:
            self._high = None
            self._emit(_REPLACEMENT)

    def _emit(self, text: str) -> None:
        if self._target is not None:
            self._target.append(text)

    def _nested_char(self, char: str) -> None:
        """Skip a structured value, respecting strings so a brace inside one doesn't count."""
        if self._nested_string:
            if self._nested_escape:
                self._nested_escape = False
            elif char == "\\":
                self._nested_escape = True
            elif char == '"':
                self._nested_string = False
            return
        if char == '"':
            self._nested_string = True
        elif char in "{[":
            self._depth += 1
        elif char in "}]":
            self._depth -= 1
            if self._depth == 0:
                self._state = _State.SEEK_KEY
