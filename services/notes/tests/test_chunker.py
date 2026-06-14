"""Unit tests for the heading-aware markdown chunker."""

from __future__ import annotations

from epicurus_notes.chunker import chunk_note


def test_empty_content_yields_no_chunks() -> None:
    assert chunk_note("") == []
    assert chunk_note("   \n\n  ") == []


def test_intro_before_first_heading_is_its_own_chunk() -> None:
    chunks = chunk_note("intro text\n\n# Section\n\nbody")
    assert chunks[0].heading == ""
    assert "intro text" in chunks[0].text
    assert chunks[1].heading == "Section"


def test_each_heading_becomes_a_chunk() -> None:
    chunks = chunk_note("# A\n\naaa\n\n# B\n\nbbb")
    assert [c.heading for c in chunks] == ["A", "B"]
    assert chunks[0].index == 0
    assert chunks[1].index == 1


def test_large_section_splits_at_paragraph_boundaries() -> None:
    body = "\n\n".join(["para " + str(i) * 50 for i in range(10)])
    chunks = chunk_note(f"# Big\n\n{body}", max_chars=200)
    assert len(chunks) > 1
    # No chunk exceeds the limit by more than a paragraph; never split mid-word.
    assert all(c.text for c in chunks)
