"""Unit tests for context compaction — trimming a conversation to fit the window.

The estimator is a deliberate heuristic, so these assert *behaviour* (what survives, in what
order, with tool-pairing intact) rather than exact token counts. Budgets are chosen relative to
the per-message estimate so the intent is clear.
"""

from __future__ import annotations

from epicurus_core_app.llm.compaction import (
    _IMAGE_TOKEN_ESTIMATE,
    compact_messages,
    estimate_tokens,
    estimate_tools_tokens,
    message_tokens,
    reply_reserve,
)
from epicurus_core_app.llm.models import ChatMessage

# A long-ish body so each non-system message costs a predictable, comparable chunk.
_BODY = "x" * 96


def _msg(role: str, tag: str, **kw: object) -> ChatMessage:
    return ChatMessage(role=role, content=f"{tag} {_BODY}", **kw)  # type: ignore[arg-type]


def _contents(messages: list[ChatMessage]) -> list[str | None]:
    return [m.content for m in messages]


# ── estimators ─────────────────────────────────────────────────────────────────────


def test_estimate_tokens_scales_with_length() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("x" * 35) == 10  # 35 / 3.5
    assert estimate_tokens("x" * 36) == 11  # rounds up (conservative)


def test_message_tokens_counts_content_and_tool_calls() -> None:
    plain = ChatMessage(role="user", content="x" * 35)
    assert message_tokens(plain) == 4 + 10  # overhead + content
    with_calls = ChatMessage(
        role="assistant",
        content="",
        tool_calls=[{"id": "1", "function": {"name": "f", "arguments": "{}"}}],
    )
    assert message_tokens(with_calls) > 4  # the tool-call JSON adds to the estimate


def test_message_tokens_counts_multimodal_image_parts() -> None:
    # A message whose content is multimodal parts (#633) must not be estimated via len() on the
    # list itself (that would count *parts*, e.g. 2, not the actual — much larger — payload).
    text_only = ChatMessage(role="user", content="describe this")
    with_image = ChatMessage(
        role="user",
        content=[
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 5000}},
        ],
    )
    assert message_tokens(with_image) == message_tokens(text_only) + _IMAGE_TOKEN_ESTIMATE
    # A large base64 payload must not itself inflate the estimate beyond the flat constant —
    # image tokens are priced by resolution, not by how many base64 characters they encode.
    assert message_tokens(with_image) < 2 * _IMAGE_TOKEN_ESTIMATE


def test_estimate_tools_tokens_zero_when_no_tools() -> None:
    assert estimate_tools_tokens(None) == 0
    assert estimate_tools_tokens([]) == 0
    assert estimate_tools_tokens([{"type": "function", "function": {"name": "search"}}]) > 0


def test_reply_reserve_is_a_bounded_quarter() -> None:
    assert reply_reserve(4096) == 1024
    assert reply_reserve(2048) == 512  # floor
    assert reply_reserve(1024) == 512  # floor
    assert reply_reserve(100_000) == 4096  # ceiling


# ── compaction ───────────────────────────────────────────────────────────────────


def test_compact_is_a_noop_when_it_already_fits() -> None:
    messages = [_msg("system", "S"), _msg("user", "U1"), _msg("assistant", "A1")]
    out = compact_messages(messages, budget=10_000, note="trimmed")
    assert _contents(out) == _contents(messages)  # unchanged, no note inserted


def test_compact_keeps_system_prefix_and_drops_oldest_turns() -> None:
    messages = [
        _msg("system", "S"),
        _msg("user", "U1"),
        _msg("assistant", "A1"),
        _msg("user", "U2"),
        _msg("assistant", "A2"),
        _msg("user", "U3"),
    ]
    per = message_tokens(messages[1])  # all non-system bodies cost the same
    sys = message_tokens(messages[0])
    # Budget room for the system message + exactly the two most-recent turns.
    out = compact_messages(messages, budget=sys + per * 2)
    tags = [c.split(" ", 1)[0] for c in _contents(out) if c]
    assert tags[0] == "S"  # system kept
    assert tags[-2:] == ["A2", "U3"]  # the two newest survive
    assert "U1" not in tags and "A1" not in tags and "U2" not in tags  # oldest dropped


def test_compact_always_keeps_at_least_the_final_message() -> None:
    messages = [_msg("system", "S"), _msg("user", "U1"), _msg("user", "LAST")]
    out = compact_messages(messages, budget=1)  # absurdly small
    tags = [c.split(" ", 1)[0] for c in _contents(out) if c]
    assert "LAST" in tags  # the user's actual question is never dropped
    assert tags[0] == "S"


def test_compact_budgets_an_image_message_as_real_weight_not_two_parts() -> None:
    # The newest turn carries an image (#633) — its cost must count against the budget for real,
    # not as if `content` (a 2-item list) were estimated the way a 2-character string would be.
    image_msg = ChatMessage(
        role="user",
        content=[
            {"type": "text", "text": "LAST"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 5000}},
        ],
    )
    messages = [_msg("system", "S"), _msg("user", "U1"), _msg("assistant", "A1"), image_msg]
    sys = message_tokens(messages[0])
    per_old_turn = message_tokens(messages[1])
    image_cost = message_tokens(image_msg)
    assert image_cost > per_old_turn * 10  # the image dwarfs an ordinary text turn, as expected

    # Enough for the system prefix + the image turn + exactly one old turn.
    out = compact_messages(messages, budget=sys + image_cost + per_old_turn)
    tags = [c.split(" ", 1)[0] for c in _contents(out) if isinstance(c, str)]
    assert out[-1].content == image_msg.content  # the image turn always survives (newest)
    assert tags == ["S", "A1"]  # exactly one old turn fits alongside it

    # Only enough for the system prefix + the image turn — no room for any old turn at all.
    tight = compact_messages(messages, budget=sys + image_cost)
    assert [m.content for m in tight] == [messages[0].content, image_msg.content]


def test_compact_never_orphans_a_tool_result() -> None:
    messages = [
        _msg("system", "S"),
        _msg("user", "U1"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[{"id": "c1", "function": {"name": "search", "arguments": "{}"}}],
        ),
        ChatMessage(role="tool", tool_call_id="c1", name="search", content=f"RESULT {_BODY}"),
        _msg("user", "U2"),
    ]
    per = message_tokens(messages[1])
    sys = message_tokens(messages[0])
    # Room for system + ~two newest entries — which from the back are (tool result, U2). Keeping
    # the tool result without its assistant call would dangle, so it must be dropped.
    out = compact_messages(messages, budget=sys + per * 2)
    roles_after_system = [m.role for m in out if m.role != "system"]
    assert roles_after_system and roles_after_system[0] != "tool"  # no orphan at the boundary
    assert not any(
        m.role == "tool" and not _has_preceding_assistant(out, i) for i, m in enumerate(out)
    )


def test_compact_keeps_a_tool_pair_together_when_both_fit() -> None:
    messages = [
        _msg("system", "S"),
        _msg("user", "U1"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[{"id": "c1", "function": {"name": "search", "arguments": "{}"}}],
        ),
        ChatMessage(role="tool", tool_call_id="c1", name="search", content="ok"),
        _msg("assistant", "A1"),
    ]
    # Generous budget that still forces dropping only U1: the assistant→tool pair stays intact.
    out = compact_messages(messages, budget=message_tokens(messages[0]) + 200)
    roles = [m.role for m in out]
    assert "tool" in roles
    tool_idx = roles.index("tool")
    assert roles[tool_idx - 1] == "assistant"  # its call precedes it


def test_compact_inserts_a_trim_note_only_when_it_drops_something() -> None:
    messages = [_msg("system", "S")] + [_msg("user", f"U{i}") for i in range(8)]
    per = message_tokens(messages[1])
    out = compact_messages(messages, budget=message_tokens(messages[0]) + per * 2, note="TRIMMED")
    assert any(m.role == "system" and m.content == "TRIMMED" for m in out)
    # The note sits right after the kept system prefix, before the surviving turns.
    note_idx = next(i for i, m in enumerate(out) if m.content == "TRIMMED")
    assert all(m.role == "system" for m in out[:note_idx])


def _has_preceding_assistant(messages: list[ChatMessage], idx: int) -> bool:
    """Whether the message at ``idx`` (a tool result) has an assistant before it in the list."""
    return any(m.role == "assistant" for m in messages[:idx])
