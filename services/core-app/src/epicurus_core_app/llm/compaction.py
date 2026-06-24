"""Fit a conversation to the model's context window before it reaches the runtime.

A local runtime (Ollama) silently drops tokens past ``num_ctx``. With the default 4096 that
happens fast: the system prompt + every module's tool schema + a few turns overflow, and the
*oldest* tokens — which include the agent's instructions and recalled context — are what get
evicted, wrecking the reply. Rather than let the runtime truncate blindly, we trim the message
list ourselves so the prompt fits the window, keeping the parts that matter most.

The policy (a deliberate, simple **cut**, not summarization):

* **Always keep the leading system messages** — the agent's instructions, recalled memory, and
  any attached-context preamble are prepended as ``system`` turns and must survive.
* **Keep the most recent turns that fit**, walking from the end; older history is dropped first.
* **Never orphan a tool result** — a ``tool`` message at the front of the kept window (whose
  ``assistant`` tool-call was dropped) is removed, since providers reject a dangling result.
* **Always keep at least the final message** (the user's question / the latest tool result),
  even if it alone is over budget — better an over-long prompt the runtime trims than a reply to
  the wrong thing.

Token counts are a deliberately conservative *estimate* (no tokenizer dependency, arbitrary
local models): characters over a fixed divisor, erring toward over-counting so we trim a little
early rather than overflow. The caller leaves a reply reserve and subtracts the tool-schema
estimate before passing the budget here.
"""

from __future__ import annotations

import json
from math import ceil

from epicurus_core_app.llm.models import ChatMessage

# Conservative chars-per-token: real English is ~4, dense JSON/code is lower (more tokens per
# char). 3.5 sits between and errs toward over-counting, which is the safe direction here.
_CHARS_PER_TOKEN = 3.5
# Flat per-message overhead (role, delimiters, tool_call_id/name) the content estimate misses.
_PER_MESSAGE_OVERHEAD = 4
# Reply reserve bounds: leave room to actually generate an answer within the window.
_MIN_REPLY_RESERVE = 512
_MAX_REPLY_RESERVE = 4096


def estimate_tokens(text: str) -> int:
    """A conservative token estimate for ``text`` (characters over a fixed divisor)."""
    if not text:
        return 0
    return ceil(len(text) / _CHARS_PER_TOKEN)


def message_tokens(message: ChatMessage) -> int:
    """Estimated tokens for one message: content + any tool-call payload + flat overhead."""
    total = _PER_MESSAGE_OVERHEAD + estimate_tokens(message.content or "")
    if message.tool_calls:
        total += estimate_tokens(json.dumps(message.tool_calls, ensure_ascii=False, default=str))
    return total


def estimate_tools_tokens(tools: list[dict[str, object]] | None) -> int:
    """Estimated tokens the tool schemas add to the prompt (they count against the window)."""
    if not tools:
        return 0
    return estimate_tokens(json.dumps(tools, ensure_ascii=False, default=str))


def reply_reserve(budget: int) -> int:
    """Tokens to hold back from the window for the model's reply (a quarter, within bounds)."""
    return min(_MAX_REPLY_RESERVE, max(_MIN_REPLY_RESERVE, budget // 4))


def compact_messages(
    messages: list[ChatMessage], *, budget: int, note: str | None = None
) -> list[ChatMessage]:
    """Trim ``messages`` to roughly ``budget`` tokens, keeping system + the most-recent turns.

    Returns the original list (a copy) untouched when it already fits — the common case. When it
    doesn't, the leading ``system`` prefix is kept whole, the newest turns that fit are kept (an
    orphaned leading ``tool`` result is dropped), and — if anything was dropped and ``note`` is
    given — a short ``system`` aside is inserted so the model knows earlier turns were trimmed.
    """
    if budget <= 0:
        return list(messages)
    if sum(message_tokens(m) for m in messages) <= budget:
        return list(messages)

    # The leading system messages (instructions, recalled memory, attached context) are kept whole.
    prefix_len = 0
    while prefix_len < len(messages) and messages[prefix_len].role == "system":
        prefix_len += 1
    system_prefix = messages[:prefix_len]
    rest = messages[prefix_len:]

    marker = ChatMessage(role="system", content=note) if note else None
    available = budget - sum(message_tokens(m) for m in system_prefix)
    if marker is not None:
        available -= message_tokens(marker)

    # Keep the newest messages that fit, walking backwards; always keep the last one.
    kept: list[ChatMessage] = []
    used = 0
    for message in reversed(rest):
        cost = message_tokens(message)
        if kept and used + cost > available:
            break
        kept.append(message)
        used += cost
    kept.reverse()

    # A tool result whose assistant tool-call we just dropped would dangle — providers reject it.
    while kept and kept[0].role == "tool":
        kept.pop(0)

    dropped = len(kept) < len(rest)
    if marker is not None and dropped:
        return [*system_prefix, marker, *kept]
    return [*system_prefix, *kept]
