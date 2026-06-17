"""Manifest tests — the notes module's declared surface.

The defining property of Notes (#134): it is **attach-only**, so it must expose an
editor page and an attach surface but **no agent tools**.
"""

from __future__ import annotations

from epicurus_notes.service import SAVED_SUBJECT, build_module


async def test_manifest_identity() -> None:
    manifest = await build_module().manifest()
    assert manifest.name == "notes"
    assert manifest.version == "0.2.0"


async def test_exposes_no_tools() -> None:
    # Attach-only: the agent has no automatic access to notes.
    manifest = await build_module().manifest()
    assert manifest.tools == []


async def test_is_attachable() -> None:
    manifest = await build_module().manifest()
    assert manifest.attachable is True


async def test_declares_editor_page() -> None:
    manifest = await build_module().manifest()
    page = next(p for p in manifest.pages if p.id == "notes")
    assert page.archetype == "editor"
    assert page.title == "Notes"


async def test_has_ui_and_emits_saved_event() -> None:
    manifest = await build_module().manifest()
    assert manifest.ui is not None
    assert manifest.ui.status_url == "/status"
    assert any(e.subject == SAVED_SUBJECT for e in manifest.events_emitted)
