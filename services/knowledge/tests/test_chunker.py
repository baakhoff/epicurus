"""Unit tests for the heading-aware markdown chunker."""

from __future__ import annotations

from epicurus_knowledge.chunker import chunk_note


def test_empty_note_returns_no_chunks() -> None:
    assert chunk_note("") == []
    assert chunk_note("   \n  ") == []


def test_no_headings_single_chunk() -> None:
    content = "This is a note with no headings.\nJust a paragraph."
    chunks = chunk_note(content)
    assert len(chunks) == 1
    assert chunks[0].heading == ""
    assert chunks[0].index == 0
    assert "no headings" in chunks[0].text


def test_headings_produce_separate_chunks() -> None:
    content = "# Introduction\n\nSome intro text.\n\n## Details\n\nMore info."
    chunks = chunk_note(content)
    headings = [c.heading for c in chunks]
    assert "Introduction" in headings
    assert "Details" in headings


def test_intro_section_captured() -> None:
    content = "Front matter text.\n\n# First Heading\n\nBody."
    chunks = chunk_note(content)
    intro = [c for c in chunks if c.heading == ""]
    assert len(intro) == 1
    assert "Front matter" in intro[0].text


def test_chunk_indices_are_sequential() -> None:
    content = "# A\n\ntext A\n\n## B\n\ntext B\n\n### C\n\ntext C"
    chunks = chunk_note(content)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_large_section_splits_at_paragraphs() -> None:
    # Build a section that exceeds 100 chars with two paragraphs.
    para1 = "A" * 60
    para2 = "B" * 60
    content = f"# BigSection\n\n{para1}\n\n{para2}"
    chunks = chunk_note(content, max_chars=100)
    # Should produce at least 2 chunks for the big section.
    assert len(chunks) >= 2


def test_chunk_text_not_empty() -> None:
    content = "# H1\n\n\n\n## H2\n\nsome text"
    chunks = chunk_note(content)
    for c in chunks:
        assert c.text.strip()


def test_deep_headings_captured() -> None:
    content = "### Level 3\n\nDeep text."
    chunks = chunk_note(content)
    assert any(c.heading == "Level 3" for c in chunks)


def test_obsidian_style_note() -> None:
    note = """\
# My Note

Some preamble about the topic.

## Background

Context and history here.

## Main Points

- Point one
- Point two

## Conclusion

Summary text.
"""
    chunks = chunk_note(note)
    headings = {c.heading for c in chunks}
    assert "My Note" in headings
    assert "Background" in headings
    assert "Main Points" in headings
    assert "Conclusion" in headings
