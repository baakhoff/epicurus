"""What the user is told when a streaming turn fails (#453, #944, #947 — ADR-0142).

A turn that dies mid-stream used to end one of two ways, both wrong. With nothing streamed
yet the banner was ``str(exc)`` — for a hosted model that is the provider's raw JSON, account
identifier and all, rendered verbatim in the browser (#947). With a partial answer already on
screen no terminal event fired at all: the reply simply stopped and the shell said nothing
(#944 part 3).

This module owns both halves of the fix:

* :func:`classify_stream_failure` turns any exception into a :class:`StreamFailure` — an
  operator-readable banner, the note appended to a retained partial answer, and a short
  ``reason`` label for the metric. **Nothing here ever returns a provider payload.** The one
  passthrough is :class:`~epicurus_core_app.llm.power.GatewayPausedError`, an explicit
  allowance because the web keys its asleep state on ``/paused/i``.
* :func:`readable_provider_message` is the redaction gate. A provider's *message* is often the
  single most useful sentence in the failure ("… is an embedding model and cannot be used with
  the chat/completions endpoint"), so it is quoted — but only after it survives a plain-sentence
  test that rejects JSON, ids, keys and anything else that is not a sentence a person wrote.

The classifier matches on the exception's **type name**, not its class, so the agent needs no
import of litellm's (or httpx's, or openai's) exception hierarchy — the same reason the
connection markers below are text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from prometheus_client import Counter

# Failed streaming turns, by why. `reason` is one of the labels in `_REASONS` below — a small
# closed set, never a provider string, so the cardinality is bounded per tenant (#944).
STREAM_FAILURES = Counter(
    "epicurus_core_llm_stream_failures_total",
    "Streaming agent turns that ended in a failure rather than an answer.",
    ["tenant", "reason"],
)

# Every value `StreamFailure.reason` can take — the metric's label domain.
_REASONS = (
    "paused",  # the gateway is asleep; not a fault
    "capability",  # the model cannot do what was asked (ModelCapabilityError)
    "stalled",  # the connection/stall class (#453)
    "rejected",  # the provider refused the request (4xx)
    "auth",  # the provider rejected the credentials
    "rate_limited",  # the provider throttled us
    "unavailable",  # the provider is down or overloaded
    "unknown",  # anything else — the generic fallthrough
)

# The connection/stall class (#453): most often the local model stopping mid-answer as it loads
# another model or evaluates a long prompt and the socket read aborts. Matched loosely, by
# exception type + message, so the agent needn't import litellm's exception types.
STREAM_CONNECTION_MARKERS = (
    "timeout",
    "timed out",
    "socket",
    "apiconnection",
    "connection",
    "midstreamfallback",
    "read error",
    "econnreset",
)

STREAM_STALLED_MESSAGE = (
    "The model stopped responding before the answer was finished — it may have been busy loading "
    "another model. Please try again."
)
STREAM_INTERRUPTED_MESSAGE = "The answer was interrupted before it finished. Please try again."

# Exception *type names* that mean "the provider answered, and said no". Mapped to the reason
# label and to whether the provider's own message is worth quoting when it survives redaction.
# Names come from litellm, httpx and the OpenAI SDK, which share most of them.
_PROVIDER_REASONS: dict[str, str] = {
    "BadRequestError": "rejected",
    "UnprocessableEntityError": "rejected",
    "ContextWindowExceededError": "rejected",
    "ContentPolicyViolationError": "rejected",
    "NotFoundError": "rejected",
    "UnsupportedParamsError": "rejected",
    "HTTPStatusError": "rejected",
    "AuthenticationError": "auth",
    "PermissionDeniedError": "auth",
    "BudgetExceededError": "auth",
    "RateLimitError": "rate_limited",
    "ServiceUnavailableError": "unavailable",
    "InternalServerError": "unavailable",
    "APIError": "unavailable",
    "APIStatusError": "unavailable",
    "Timeout": "stalled",
}

_PICK_ANOTHER = "Pick another model on the Models page, or try again."


@dataclass(frozen=True)
class StreamFailure:
    """How one failed streaming turn is reported.

    ``banner`` is the terminal ``error`` event's detail — what the shell puts in its danger
    card. ``note`` is appended to a *retained* partial answer, so the reason survives into the
    persisted transcript and is still there after a reload. ``reason`` is the metric label.
    """

    banner: str
    note: str
    reason: str


def _model_name(model: str | None) -> str:
    """How a model is named in a sentence — its id, or a neutral noun when we have none."""
    return model or "The model"


def _provider_name(exc: Exception, model: str | None) -> str | None:
    """The provider to name in a sentence, most specific first.

    An aggregator names the upstream it routed to in the payload (``"provider_name":"NextBit"``)
    — that is the one the operator has to act on, so it wins over litellm's own routing label
    and over the model id's prefix. Never an account id, a base URL or a key: whatever is picked
    has to pass :func:`_is_plain_token` first.
    """
    routed = _PROVIDER_NAME_RE.search(str(exc))
    if routed is not None and _is_plain_token(routed.group(1)):
        return routed.group(1)
    provider = getattr(exc, "llm_provider", None)
    if isinstance(provider, str) and _is_plain_token(provider.strip()):
        return provider.strip()
    if model and "/" in model:
        head = model.split("/", 1)[0]
        return head if _is_plain_token(head) else None
    return None


# The upstream an aggregator routed to, as it reports itself in the failure payload.
_PROVIDER_NAME_RE = re.compile(r'"provider_name"\s*:\s*"([^"]{1,40})"')


def _is_plain_token(value: str) -> bool:
    """A short, boring identifier — letters, digits, dot, dash, underscore."""
    return bool(value) and len(value) <= 40 and re.fullmatch(r"[A-Za-z0-9._-]+", value) is not None


# ── redaction ────────────────────────────────────────────────────────────────────────────────

# Where a `"message": "` value starts, and the first plausible place it ends — a quote that is
# followed by the next key or by the close of the object. Deliberately more forgiving than
# strict JSON: the payloads this has to read are escaped by two or three intermediaries and
# #947's real one is not valid JSON at any depth (vLLM's own quotes are left unescaped inside
# it). Every occurrence is scanned independently rather than with one `finditer` over the whole
# blob, so an outer message whose value *contains* the inner one does not swallow it.
_MESSAGE_KEY_RE = re.compile(r'"message"\s*:\s*"')
_MESSAGE_END_RE = re.compile(r'"\s*(?:,\s*"|\}|$)')

# How deep the escaping is unwound. #947's real payload nests three levels (OpenRouter wrapping
# a provider wrapping vLLM); four is one more than anything seen.
_MAX_UNESCAPE_DEPTH = 4

# Substrings that disqualify a candidate sentence outright — anything that smells like an
# identity, a credential or a routing detail the operator did not ask for.
_FORBIDDEN_SUBSTRINGS = (
    "user_id",
    "userid",
    "api_key",
    "apikey",
    "api key",
    "access_token",
    "authorization",
    "bearer ",
    "secret",
    "account_id",
    "org_id",
    "organization_id",
    "session_id",
    "request_id",
    "trace_id",
)

# A long run that mixes letters and digits — a uuid, a hash, an api key. One is enough to
# reject the candidate. The digit requirement is what keeps a legitimate long flag name out of
# it: `--enable-auto-tool-choice` (25 characters, no digit) is part of a real refusal message.
_ID_RUN_RE = re.compile(r"\b(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{20,}\b")

# How much of a provider's sentence is worth quoting. Longer than this is a dump, not a message.
_MESSAGE_CAP = 240


def _unescape_once(text: str) -> str:
    """Undo one level of backslash escaping, textually (``\\"`` → ``"``, ``\\\\`` → ``\\``)."""
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def is_plain_sentence(candidate: str) -> bool:
    """Whether *candidate* is something a person wrote, safe to show a user.

    Rejects structure (braces, brackets, JSON punctuation, escapes), identity (the forbidden
    substrings and any long id-like run), and anything too short to be a sentence or too long
    to be a message.
    """
    text = candidate.strip()
    if not (8 <= len(text) <= _MESSAGE_CAP):
        return False
    if " " not in text:
        return False
    if any(bad in text for bad in ("{", "}", "[", "]", "\\", '":', "':")):
        return False
    lowered = text.lower()
    if any(bad in lowered for bad in _FORBIDDEN_SUBSTRINGS):
        return False
    if _ID_RUN_RE.search(text):
        return False
    # Mostly words: a sentence is not a base64 blob with spaces in it.
    letters = sum(1 for c in text if c.isalpha() or c.isspace())
    return letters >= len(text) * 0.6


def readable_provider_message(blob: str) -> str | None:
    """The deepest human-readable ``message`` inside a provider payload, or ``None``.

    Providers wrap each other: OpenRouter reports "Provider returned error" and carries the
    upstream's real refusal escaped inside ``metadata.raw`` — sometimes twice over. Unwinding
    one escape level at a time and keeping the *deepest* candidate that passes
    :func:`is_plain_sentence` yields the sentence the operator actually needs
    ("… tool choice requires --enable-auto-tool-choice …") rather than the outer placeholder.
    Nothing that fails the test is ever returned, so a payload with no readable sentence in it
    produces no quotation at all.
    """
    best: str | None = None
    text = blob
    for _ in range(_MAX_UNESCAPE_DEPTH):
        for candidate in _message_values(text):
            if is_plain_sentence(candidate):
                best = candidate
        nxt = _unescape_once(text)
        if nxt == text:
            break
        text = nxt
    return best


def _message_values(text: str) -> list[str]:
    """Every ``"message": "…"`` value in *text*, outermost first, nested ones included."""
    values: list[str] = []
    for key in _MESSAGE_KEY_RE.finditer(text):
        end = _MESSAGE_END_RE.search(text, key.end())
        if end is not None:
            values.append(text[key.end() : end.start()].strip())
    return values


# ── classification ───────────────────────────────────────────────────────────────────────────


def classify_stream_failure(exc: Exception, *, model: str | None = None) -> StreamFailure:
    """Turn a mid-stream exception into what the user is told (ADR-0142).

    Order matters. The gateway's own paused signal passes through verbatim (the web parses it);
    a capability refusal already carries a written message and hint; the connection/stall class
    keeps its #453 wording; a provider rejection gets a sentence naming the model and, where
    known, the provider, quoting the provider's message only when it survives redaction; and
    anything left names its exception class and nothing else. ``str(exc)`` is never the banner
    outside the paused allowance.
    """
    kind = type(exc).__name__

    # The one deliberate passthrough: `stores/chat.ts` tests /paused/i against the detail to
    # show the asleep card. Keeping it explicit (by type) is what lets everything else be safe.
    if kind == "GatewayPausedError":
        return StreamFailure(banner=str(exc), note=STREAM_INTERRUPTED_MESSAGE, reason="paused")

    # Lane-A's typed capability refusal (ADR-0140): already written for a person, no payload.
    message = getattr(exc, "message", None)
    hint = getattr(exc, "hint", None)
    if kind == "ModelCapabilityError" and isinstance(message, str) and isinstance(hint, str):
        text = f"{message.rstrip('.')}. {hint.rstrip('.')}."
        return StreamFailure(banner=text, note=text, reason="capability")

    blob = f"{kind}: {exc}"
    lowered = blob.lower()
    if any(marker in lowered for marker in STREAM_CONNECTION_MARKERS):
        return StreamFailure(
            banner=STREAM_STALLED_MESSAGE, note=STREAM_STALLED_MESSAGE, reason="stalled"
        )

    reason = _PROVIDER_REASONS.get(kind)
    if reason is not None:
        text = _provider_sentence(exc, reason=reason, model=model)
        return StreamFailure(banner=text, note=text, reason=reason)

    # Unknown: name the class so a log line and a bug report agree, and nothing else.
    text = f"{_model_name(model)} failed with an unexpected error ({kind}). {_PICK_ANOTHER}"
    return StreamFailure(banner=text, note=text, reason="unknown")


def _provider_sentence(exc: Exception, *, reason: str, model: str | None) -> str:
    """One operator-readable sentence for a provider refusal, naming model and provider."""
    name = _model_name(model)
    provider = _provider_name(exc, model)
    where = f" (provider: {provider})" if provider else ""
    if reason == "auth":
        return (
            f"The provider rejected the credentials for {name}{where}. "
            "Check the API key for that provider on the Models page."
        )
    if reason == "rate_limited":
        return (
            f"{name} is rate-limited by its provider{where}. "
            "Wait a moment and try again, or pick another model on the Models page."
        )
    if reason == "unavailable":
        return f"{name}'s provider is unavailable right now{where}. {_PICK_ANOTHER}"
    quoted = readable_provider_message(str(exc))
    if quoted:
        return f"{name} was rejected by its provider{where} — {quoted.rstrip('.')}."
    return f"{name} was rejected by its provider{where}. {_PICK_ANOTHER}"
