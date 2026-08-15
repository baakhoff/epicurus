"""The document-preview tracker (#654, ADR-0121) — what gets coalesced, and what never leaks.

The tracker sits between the gateway's tool-call fragments and the ``doc_preview`` events the
pane draws. Its contract has three halves: only annotated calls produce previews, the throttle
bounds how much of the stream a document may take, and nothing it does can cost the turn.

The clock is injected, so every timing assertion here is exact rather than slept-for.
"""

from __future__ import annotations

import json

from epicurus_core import WritesDocument
from epicurus_core_app.agent.doc_preview import (
    PREVIEW_INTERVAL_S,
    PREVIEW_MAX_CHARS,
    DocumentPreviewTracker,
    DocumentToolLookup,
    PreviewFrame,
)
from epicurus_core_app.llm.models import ToolCallFragment


class _Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _lookup(
    *,
    module: str = "knowledge",
    tool: str = "write_doc",
    spec: WritesDocument | None = None,
) -> DocumentToolLookup:
    annotation = spec or WritesDocument(content_arg="content", target_arg="path", title_arg="title")

    async def lookup(name: str) -> tuple[str, WritesDocument] | None:
        return (module, annotation) if name == tool else None

    return lookup


def _fragment(
    arguments: str | None = None, *, name: str | None = "write_doc", slot: int = 0
) -> ToolCallFragment:
    return ToolCallFragment(slot=slot, id="call_1", name=name, arguments=arguments)


def _tracker(
    lookup: DocumentToolLookup | None = None, clock: _Clock | None = None
) -> tuple[DocumentPreviewTracker, _Clock]:
    ticker = clock or _Clock()
    return DocumentPreviewTracker(lookup or _lookup(), clock=ticker), ticker


# ── which calls produce a preview at all ─────────────────────────────────────


async def test_an_annotated_call_previews_its_body() -> None:
    tracker, _ = _tracker()
    frame = await tracker.feed(_fragment('{"path": "a.md", "content": "# Goa'))
    assert frame is not None
    assert frame.tool == "write_doc"
    assert frame.module == "knowledge"
    assert frame.text == "# Goa"


async def test_an_unannotated_call_previews_nothing_ever() -> None:
    # Opt-in per tool: a `search` call whose arguments happen to contain a `content` field is
    # not a document, and must never open a pane.
    tracker, clock = _tracker()
    for chunk in ['{"content": "not a ', 'document"}']:
        assert await tracker.feed(_fragment(chunk, name="search")) is None
        clock.advance(PREVIEW_INTERVAL_S)
    assert tracker.flush() == []


async def test_no_lookup_configured_means_no_previews() -> None:
    tracker = DocumentPreviewTracker(None)
    assert await tracker.feed(_fragment('{"content": "body"}')) is None
    assert tracker.flush() == []


async def test_a_failing_lookup_costs_the_preview_not_the_turn() -> None:
    async def exploding(_name: str) -> tuple[str, WritesDocument] | None:
        raise RuntimeError("registry down")

    tracker, _ = _tracker(exploding)
    assert await tracker.feed(_fragment('{"content": "body"}')) is None
    assert tracker.flush() == []


async def test_the_lookup_runs_once_per_call_not_once_per_fragment() -> None:
    calls: list[str] = []

    async def counting(name: str) -> tuple[str, WritesDocument] | None:
        calls.append(name)
        return "knowledge", WritesDocument(content_arg="content")

    tracker, clock = _tracker(counting)
    for chunk in ['{"content": "a', "b", "c", 'd"}']:
        clock.advance(PREVIEW_INTERVAL_S)
        await tracker.feed(_fragment(chunk))
    assert calls == ["write_doc"]


async def test_argument_text_that_arrives_before_the_name_is_not_lost() -> None:
    # Ordering insurance: providers name a call on its first fragment, but nothing in the
    # contract says they must.
    tracker, _ = _tracker()
    assert await tracker.feed(_fragment('{"content": "early', name=None)) is None
    frame = await tracker.feed(_fragment(' text"}'))
    assert frame is not None and frame.text == "early text"


async def test_two_concurrent_calls_keep_their_own_bodies() -> None:
    async def lookup(name: str) -> tuple[str, WritesDocument] | None:
        if name == "write_doc":
            return "knowledge", WritesDocument(content_arg="content")
        if name == "write_note":
            return "notes", WritesDocument(content_arg="body")
        return None

    tracker, clock = _tracker(lookup)
    first = await tracker.feed(
        ToolCallFragment(slot=0, name="write_doc", arguments='{"content": "doc body"}')
    )
    second = await tracker.feed(
        ToolCallFragment(slot=1, name="write_note", arguments='{"body": "note body"}')
    )
    clock.advance(1)
    assert first is not None and (first.module, first.text) == ("knowledge", "doc body")
    assert second is not None and (second.module, second.text) == ("notes", "note body")


# ── the throttle (requirement 4's policy) ────────────────────────────────────


async def test_the_first_slice_goes_out_immediately() -> None:
    # The pane should appear as the write starts, not one coalesce window later.
    tracker, _ = _tracker()
    assert await tracker.feed(_fragment('{"content": "first')) is not None


async def test_slices_inside_the_window_are_withheld_and_coalesced() -> None:
    tracker, clock = _tracker()
    assert await tracker.feed(_fragment('{"content": "a')) is not None
    clock.advance(PREVIEW_INTERVAL_S / 4)
    assert await tracker.feed(_fragment("b")) is None
    clock.advance(PREVIEW_INTERVAL_S / 4)
    assert await tracker.feed(_fragment("c")) is None
    clock.advance(PREVIEW_INTERVAL_S)
    frame = await tracker.feed(_fragment("d"))
    assert frame is not None
    assert frame.text == "bcd"  # everything withheld, in one frame, in order


async def test_a_burst_larger_than_the_size_cap_does_not_wait_for_the_window() -> None:
    # Bounds frame *size*: a model that dumps a whole document inside 100 ms would otherwise
    # produce one enormous frame.
    tracker, clock = _tracker()
    assert await tracker.feed(_fragment('{"content": "x')) is not None
    clock.advance(PREVIEW_INTERVAL_S / 10)
    frame = await tracker.feed(_fragment("y" * PREVIEW_MAX_CHARS))
    assert frame is not None
    assert len(frame.text) == PREVIEW_MAX_CHARS


async def test_flush_releases_the_tail_the_window_was_holding() -> None:
    tracker, clock = _tracker()
    await tracker.feed(_fragment('{"content": "start'))
    clock.advance(PREVIEW_INTERVAL_S / 10)
    assert await tracker.feed(_fragment(' and end"}')) is None  # withheld
    [tail] = tracker.flush()
    assert tail.text == " and end"
    assert tracker.flush() == []  # nothing left to say


async def test_no_frame_is_ever_empty() -> None:
    tracker, clock = _tracker()
    await tracker.feed(_fragment('{"content": "body"}'))
    clock.advance(10)
    assert await tracker.feed(_fragment(", ")) is None  # JSON structure, no body characters
    assert tracker.flush() == []


async def test_the_frames_concatenate_to_exactly_what_the_model_wrote() -> None:
    body = "# Report\n\n" + ('Some "quoted" prose with a 🚀 and a \\path\\. ' * 80)
    source = json.dumps({"path": "notes/r.md", "content": body})
    chunks = [source[i : i + 7] for i in range(0, len(source), 7)]
    tracker, clock = _tracker()
    frames: list[PreviewFrame] = []
    for chunk in chunks:
        clock.advance(PREVIEW_INTERVAL_S / 20)  # a fast model: most slices get coalesced
        if (frame := await tracker.feed(_fragment(chunk))) is not None:
            frames.append(frame)
    frames.extend(tracker.flush())
    assert "".join(f.text for f in frames) == body
    # …and the coalescing actually did something: far fewer frames than fragments.
    assert len(frames) < len(chunks) / 10


# ── the header: complete values only ─────────────────────────────────────────


async def test_a_half_typed_target_is_withheld_until_it_closes() -> None:
    # A header that rewrote itself character by character would be worse than a late one.
    tracker, clock = _tracker()
    first = await tracker.feed(_fragment('{"content": "body'))
    assert first is not None and first.target is None
    clock.advance(PREVIEW_INTERVAL_S)
    mid = await tracker.feed(_fragment(' more", "path": "notes/a'))
    assert mid is not None
    assert mid.text == " more"
    assert mid.target is None  # half a path is not a path


async def test_metadata_settling_after_the_body_is_left_to_the_authoritative_frame() -> None:
    # A frame only fires when there are body characters to show. A target that arrives *after*
    # the body closed therefore never rides a preview — and needs not to: v1's `tool` frame
    # carries the fully parsed arguments a moment later, and it is the one that settles the pane.
    tracker, clock = _tracker()
    assert await tracker.feed(_fragment('{"content": "done"')) is not None
    clock.advance(PREVIEW_INTERVAL_S)
    assert await tracker.feed(_fragment(', "path": "notes/a.md"}')) is None
    assert tracker.flush() == []


async def test_a_settled_target_and_title_ride_every_later_frame() -> None:
    tracker, clock = _tracker()
    await tracker.feed(_fragment('{"path": "notes/a.md", "title": "Goals", "content": "one'))
    clock.advance(PREVIEW_INTERVAL_S)
    frame = await tracker.feed(_fragment(' two"}'))
    assert frame is not None
    assert (frame.target, frame.title) == ("notes/a.md", "Goals")


async def test_an_annotation_without_a_title_argument_reports_none() -> None:
    tracker, _ = _tracker(_lookup(spec=WritesDocument(content_arg="content", target_arg="path")))
    frame = await tracker.feed(_fragment('{"path": "a.md", "content": "body"}'))
    assert frame is not None
    assert frame.title is None
    assert frame.target == "a.md"


# ── never fatal ──────────────────────────────────────────────────────────────


async def test_malformed_argument_json_keeps_what_it_read_and_stops() -> None:
    tracker, clock = _tracker()
    frame = await tracker.feed(_fragment('{"content": "readable", "x" "broken"'))
    assert frame is not None and frame.text == "readable"
    clock.advance(PREVIEW_INTERVAL_S)
    assert await tracker.feed(_fragment('{"content": "nonsense"}')) is None


async def test_a_provider_that_sends_whole_arguments_as_a_dict_previews_nothing() -> None:
    # The gateway reports no `arguments` delta for that flavour (there is nothing incremental
    # about it) — such a call simply has no typewriter, and v1's settle path still shows it.
    tracker, _ = _tracker()
    assert await tracker.feed(_fragment(None)) is None
    assert tracker.flush() == []


async def test_a_call_whose_body_has_not_started_opens_no_pane() -> None:
    # v1's rule, kept: the annotation promises the argument exists, not that the model has
    # begun filling it. An empty pane is worse than no pane.
    tracker, _ = _tracker()
    assert await tracker.feed(_fragment('{"path": "a.md", "content": "')) is None
    assert tracker.flush() == []
