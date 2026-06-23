"""Unit tests for the <think> reasoning splitter (ADR-0041)."""

from __future__ import annotations

from epicurus_core_app.llm.reasoning import ThinkSplitter, split_reasoning


def test_split_whole_message_separates_think_span() -> None:
    answer, thinking = split_reasoning("<think>let me see</think>The answer is 42.")
    assert thinking == "let me see"
    assert answer == "The answer is 42."


def test_plain_message_is_all_answer() -> None:
    answer, thinking = split_reasoning("just a normal reply")
    assert answer == "just a normal reply"
    assert thinking == ""


def test_unclosed_think_keeps_everything_as_thinking() -> None:
    # A truncated turn that never closed its <think> — the tail is reasoning, not answer.
    answer, thinking = split_reasoning("<think>still pondering")
    assert answer == ""
    assert thinking == "still pondering"


def test_streaming_reassembles_answer_and_thinking_across_chunks() -> None:
    splitter = ThinkSplitter()
    answer_parts: list[str] = []
    thinking_parts: list[str] = []
    # The <think> tags are deliberately split across chunk boundaries.
    for piece in ["<th", "ink>wei", "gh it</thi", "nk>Do ", "it."]:
        a, t = splitter.feed(piece)
        answer_parts.append(a)
        thinking_parts.append(t)
    a, t = splitter.flush()
    answer_parts.append(a)
    thinking_parts.append(t)
    assert "".join(thinking_parts) == "weigh it"
    assert "".join(answer_parts) == "Do it."


def test_held_partial_tag_is_not_leaked_into_the_answer() -> None:
    # A trailing "<" that *might* begin a tag is held back, not emitted mid-stream.
    splitter = ThinkSplitter()
    answer, thinking = splitter.feed("hello <")
    assert answer == "hello "  # the "<" is held pending the next chunk
    assert thinking == ""
    # It turns out to be a literal "<" — released as answer at flush.
    answer2, thinking2 = splitter.flush()
    assert answer2 == "<"
    assert thinking2 == ""


def test_multiple_think_spans_in_one_message() -> None:
    answer, thinking = split_reasoning("<think>a</think>X<think>b</think>Y")
    assert thinking == "ab"
    assert answer == "XY"
