"""The incremental argument reader (#654, ADR-0121).

The document typewriter stands entirely on this: read one named argument out of tool-call JSON
that is still being typed. Its whole risk surface is *boundaries* — a fragment can split a
``\\`` from what it escapes, a ``\\uXXXX`` from its digits, a surrogate pair down the middle — so
most of these tests feed the same JSON at **every** possible split point and demand one answer.
"""

from __future__ import annotations

import json
import random

from epicurus_core_app.agent.partial_json import StreamingArguments


def _read(*chunks: str, key: str = "content", track: tuple[str, ...] = ()) -> StreamingArguments:
    reader = StreamingArguments(*(track or (key,)))
    for chunk in chunks:
        reader.feed(chunk)
    return reader


def _every_split(source: str, key: str = "content") -> set[str]:
    """Feed *source* split at every position (and one character at a time); collect the answers.

    One entry in the returned set means the reader is split-invariant — the property the whole
    module has to have, since a provider chooses the boundaries and we do not.
    """
    answers = {_read(source, key=key).text(key)}
    for cut in range(len(source) + 1):
        answers.add(_read(source[:cut], source[cut:], key=key).text(key))
    answers.add(_read(*source, key=key).text(key))
    return answers


# ── the ordinary cases ───────────────────────────────────────────────────────


def test_reads_a_complete_value() -> None:
    reader = _read('{"path": "a.md", "content": "hello"}')
    assert reader.text("content") == "hello"
    assert reader.closed("content")
    assert not reader.broken


def test_reads_a_value_that_is_still_being_typed() -> None:
    reader = _read('{"path": "a.md", "content": "# Goa')
    assert reader.text("content") == "# Goa"
    assert not reader.closed("content")  # the pane must know the body is not final
    assert not reader.broken


def test_is_indifferent_to_where_the_fragments_break() -> None:
    source = '{"path": "notes/a.md", "content": "one two three", "title": "T"}'
    assert _every_split(source) == {"one two three"}


def test_an_absent_key_reads_as_empty_not_missing() -> None:
    reader = _read('{"path": "a.md"}')
    assert reader.text("content") == ""
    assert not reader.closed("content")


def test_an_empty_body_is_closed_and_empty() -> None:
    reader = _read('{"content": ""}')
    assert reader.text("content") == ""
    assert reader.closed("content")


def test_a_non_string_value_yields_nothing() -> None:
    # The annotation promises the argument exists, not that the model filled it with a string.
    reader = _read('{"content": 42}')
    assert reader.text("content") == ""
    assert not reader.broken


def test_tracks_several_keys_at_once() -> None:
    reader = _read(
        '{"path": "a.md", "title": "Goals", "content": "body"}',
        track=("content", "path", "title"),
    )
    assert reader.text("path") == "a.md"
    assert reader.text("title") == "Goals"
    assert reader.text("content") == "body"
    assert all(reader.closed(k) for k in ("content", "path", "title"))


def test_pretty_printed_json_reads_the_same() -> None:
    source = '{\n  "path" : "a.md" ,\n  "content" : "body"\n}'
    assert _every_split(source) == {"body"}


# ── escapes, and escapes cut in half ─────────────────────────────────────────


def test_decodes_every_simple_escape() -> None:
    source = r'{"content": "q\"q \\ \/ \b \f \n \r \t"}'
    assert _read(source).text("content") == json.loads(source)["content"]


def test_a_backslash_split_from_what_it_escapes() -> None:
    # The classic: the fragment ends on the backslash. Emitting it would print a stray `\`.
    reader = _read('{"content": "say \\', '"hi\\" now"}')
    assert reader.text("content") == 'say "hi" now'
    assert reader.closed("content")


def test_a_quote_escape_never_ends_the_value_early() -> None:
    source = r'{"content": "a \" b", "path": "after.md"}'
    assert _every_split(source) == {'a " b'}


def test_decodes_a_unicode_escape_split_anywhere() -> None:
    source = r'{"content": "café au lait"}'
    assert _every_split(source) == {"café au lait"}


def test_decodes_a_surrogate_pair_split_anywhere() -> None:
    # A four-byte character arrives as *two* escapes; a boundary between them must not produce
    # a lone surrogate — Python tolerates one in a str and then fails to encode the SSE frame.
    source = r'{"content": "ship 🚀 it"}'
    assert _every_split(source) == {"ship 🚀 it"}


def test_a_held_high_surrogate_is_never_handed_out_early() -> None:
    reader = _read(r'{"content": "x\ud83d')
    assert reader.text("content") == "x"  # the pair is incomplete — hold it, don't guess
    reader.feed(r'\ude80"}')
    assert reader.text("content") == "x🚀"


def test_an_unpaired_high_surrogate_degrades_to_a_replacement_character() -> None:
    reader = _read(r'{"content": "a\ud83dz"}')
    assert reader.text("content") == "a�z"
    assert reader.closed("content")


def test_an_unpaired_low_surrogate_degrades_to_a_replacement_character() -> None:
    assert _read(r'{"content": "a\ude80b"}').text("content") == "a�b"


def test_a_high_surrogate_at_the_very_end_flushes_on_close() -> None:
    assert _read(r'{"content": "a\ud83d"}').text("content") == "a�"


def test_literal_multibyte_characters_survive_any_split() -> None:
    source = '{"content": "café — 🚀 déjà vu"}'
    assert _every_split(source) == {"café — 🚀 déjà vu"}


# ── the traps a regex would fall into ────────────────────────────────────────


def test_a_key_name_appearing_as_another_value_is_not_the_value() -> None:
    reader = _read('{"title": "content", "content": "real"}')
    assert reader.text("content") == "real"


def test_the_key_name_inside_the_body_is_just_text() -> None:
    reader = _read('{"content": "the word content appears here"}')
    assert reader.text("content") == "the word content appears here"


def test_a_nested_objects_key_of_the_same_name_is_ignored() -> None:
    source = '{"meta": {"content": "nope", "deep": {"content": "also nope"}}, "content": "yes"}'
    assert _every_split(source) == {"yes"}


def test_an_array_value_with_braces_and_strings_inside_is_skipped() -> None:
    source = '{"tags": ["a}b", {"content": "no"}, "c,d"], "content": "yes"}'
    assert _every_split(source) == {"yes"}


def test_scalars_of_every_shape_are_skipped() -> None:
    source = '{"n": -1.5e3, "t": true, "f": false, "z": null, "content": "yes"}'
    assert _every_split(source) == {"yes"}


def test_a_duplicate_key_never_rewinds_what_was_already_handed_out() -> None:
    # The consumer has already been given the first value's characters; a second occurrence
    # must not restart the body under it.
    reader = _read('{"content": "first", "content": "second"}')
    assert reader.text("content") == "first"


# ── incremental delivery ─────────────────────────────────────────────────────


def test_drain_hands_out_each_character_exactly_once() -> None:
    reader = StreamingArguments("content")
    reader.feed('{"content": "one ')
    assert reader.drain("content") == "one "
    assert reader.drain("content") == ""  # nothing new
    reader.feed("two ")
    assert reader.drain("content") == "two "
    reader.feed('three"}')
    assert reader.drain("content") == "three"
    assert reader.text("content") == "one two three"  # the full value is still there


def test_pending_counts_only_what_has_not_been_drained() -> None:
    reader = StreamingArguments("content")
    reader.feed('{"content": "abcde')
    assert reader.pending("content") == 5
    reader.drain("content")
    assert reader.pending("content") == 0
    reader.feed("fg")
    assert reader.pending("content") == 2


def test_pending_and_drain_are_safe_for_an_untracked_key() -> None:
    reader = StreamingArguments("content")
    assert reader.pending("nope") == 0
    assert reader.drain("nope") == ""
    assert reader.text("nope") == ""
    assert not reader.closed("nope")


def test_closed_flips_only_when_the_value_terminates() -> None:
    reader = StreamingArguments("content")
    reader.feed('{"content": "half')
    assert not reader.closed("content")
    reader.feed('"')
    assert reader.closed("content")


# ── never fatal ──────────────────────────────────────────────────────────────


def test_malformed_json_marks_broken_and_keeps_what_was_decoded() -> None:
    reader = StreamingArguments("content")
    reader.feed('{"content": "kept", "next" "no colon"}')
    assert reader.text("content") == "kept"
    assert reader.broken
    reader.feed(', "content": "more"')  # further text is ignored, not misread
    assert reader.text("content") == "kept"


def test_a_stray_separator_is_tolerated_rather_than_treated_as_a_failure() -> None:
    # Being strict here would buy nothing: the body is already correct, and refusing it would
    # cost the preview over punctuation the real parse (on the finished JSON) will judge anyway.
    reader = _read('{"content": "kept", , }')
    assert reader.text("content") == "kept"
    assert not reader.broken


def test_arguments_that_are_not_an_object_are_refused_quietly() -> None:
    reader = _read('["content", "not an object"]')
    assert reader.broken
    assert reader.text("content") == ""


def test_text_after_the_closing_brace_is_ignored() -> None:
    # The "Extra data" shape from #324, seen from this side: a second object concatenated onto
    # the first must not append to the body.
    reader = _read('{"content": "first"}{"content": "second"}')
    assert reader.text("content") == "first"


def test_a_bad_unicode_escape_breaks_without_raising() -> None:
    reader = _read(r'{"content": "a\uZZZZ"}')
    assert reader.broken
    assert reader.text("content") == "a"


def test_empty_feeds_are_no_ops() -> None:
    reader = StreamingArguments("content")
    reader.feed("")
    reader.feed('{"content": "x"}')
    reader.feed("")
    assert reader.text("content") == "x"


# ── the fuzz: real JSON, random boundaries ───────────────────────────────────


def test_random_fragmentation_of_realistic_arguments_always_decodes_exactly() -> None:
    """Whatever the model writes and however the provider chops it, the body must come out."""
    rng = random.Random(654)
    bodies = [
        '# Goals\n\n- ship it\n- "quote" it\n\tindented\\path',
        "café 🚀 déjà — ünïcøde ✓",
        'nested {"json": "inside a string"} and [brackets]',
        "".join(chr(rng.randrange(32, 0x2FFF)) for _ in range(400)),
        "line\nbreaks\r\nand\ttabs everywhere " * 40,
    ]
    for body in bodies:
        source = json.dumps({"path": "notes/a.md", "content": body, "title": "T"})
        for _ in range(20):
            reader = StreamingArguments("content", "path", "title")
            cursor = 0
            while cursor < len(source):
                step = rng.randrange(1, 9)
                reader.feed(source[cursor : cursor + step])
                cursor += step
            assert reader.text("content") == body
            assert reader.closed("content")
            assert reader.text("path") == "notes/a.md"
            assert not reader.broken


def test_drained_deltas_concatenate_to_the_body_under_random_fragmentation() -> None:
    """What the pane actually receives — a sequence of deltas — must rebuild the document."""
    rng = random.Random(541)
    body = "# Long\n\n" + ('paragraph with "quotes", émojis 🚀 and \\slashes\\. ' * 60)
    source = json.dumps({"content": body})
    for _ in range(25):
        reader = StreamingArguments("content")
        seen: list[str] = []
        cursor = 0
        while cursor < len(source):
            step = rng.randrange(1, 17)
            reader.feed(source[cursor : cursor + step])
            cursor += step
            if rng.random() < 0.5:  # drain at unpredictable moments, like the throttle does
                seen.append(reader.drain("content"))
        seen.append(reader.drain("content"))
        assert "".join(seen) == body
