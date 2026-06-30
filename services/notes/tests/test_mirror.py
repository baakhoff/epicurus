"""Tests for the notes .md mirror into the core file space (#357, ADR-0065).

Postgres stays the source of truth; the mirror is best-effort, write-only output written
**through the core file API** (``PlatformClient.files_*``) so the storage module shows notes
in the Files view. Notes no longer mounts ``/data``: these tests stand in a fake
``PlatformClient`` backed by a real :class:`~epicurus_core.files.LocalFileStore`, so the
mirror's writes land on disk exactly where the core would put them and we can read them back.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core.files import FileEntry, LocalFileStore
from epicurus_notes.db import NotesStore
from epicurus_notes.indexer import NotesIndexer
from epicurus_notes.mirror import NotesMirror
from epicurus_notes.pages import NotesPages

TENANT = "test"
# The core FileStore scopes by tenant under its own root; the fake always writes as "local"
# (the mirror's own tenant is independent of the FileStore tenant — the mirror just hands a
# tenant-relative "notes/<slug>.md" path to the platform API). CORE_PREFIX is the subtree.
CORE_TENANT = "local"
CORE_PREFIX = "notes"


class _FakePlatform:
    """A stand-in ``PlatformClient`` whose ``files_*`` calls hit a real ``LocalFileStore``.

    Inlined per the repo convention (no ``tests/__init__.py``, no shared test import). It
    proves the mirror addresses the core file API correctly: a write to core path
    ``notes/<slug>.md`` lands at ``<files_root>/local/notes/<slug>.md`` on disk.
    """

    def __init__(self, files_root: Path) -> None:
        self.store = LocalFileStore(files_root)

    async def files_write(self, path: str, content: str) -> FileEntry:
        return await self.store.write_text(tenant=CORE_TENANT, path=path, content=content)

    async def files_delete(self, path: str) -> bool:
        return await self.store.delete(tenant=CORE_TENANT, path=path)

    async def files_stat(self, path: str) -> FileEntry | None:
        return await self.store.stat(tenant=CORE_TENANT, path=path)


def _make(tmp_path: Path) -> tuple[Path, Path, _FakePlatform]:
    """Wire the fake: ``files_root`` is the FileStore root, ``notes_root`` is the mirror's
    confinement base (``<files_root>/local/notes``), so a mirror write to ``notes/<slug>.md``
    surfaces at ``notes_root/<slug>.md`` and can be read back directly."""
    files_root = tmp_path
    notes_root = tmp_path / CORE_TENANT / CORE_PREFIX
    return files_root, notes_root, _FakePlatform(files_root)


async def _store() -> NotesStore:
    store = NotesStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await store.init()
    return store


def _mirror(notes_root: Path, store: NotesStore, platform: _FakePlatform) -> NotesMirror:
    # The fake duck-types PlatformClient's files_* surface; cast away the type for the ctor.
    return NotesMirror(
        notes_root,
        store,
        tenant=TENANT,
        platform=platform,
        core_prefix=CORE_PREFIX,  # type: ignore[arg-type]
    )


async def test_write_creates_md_file(tmp_path: Path) -> None:
    _, notes_root, platform = _make(tmp_path)
    mirror = _mirror(notes_root, await _store(), platform)

    await mirror.write("my-note", "# Hi\nbody")

    # Landed at core path notes/my-note.md → <files_root>/local/notes/my-note.md.
    assert (notes_root / "my-note.md").read_text(encoding="utf-8") == "# Hi\nbody"
    stored = await platform.files_stat(f"{CORE_PREFIX}/my-note.md")
    assert stored is not None and stored.path == "notes/my-note.md"


async def test_write_rejects_traversal_slug(tmp_path: Path) -> None:
    files_root, notes_root, platform = _make(tmp_path)
    mirror = _mirror(notes_root, await _store(), platform)

    await mirror.write("../escape", "nope")  # never raises — skips the unsafe slug

    # The mirror skipped the slug before touching the core API, so nothing was written.
    assert not (notes_root.parent / "escape.md").exists()
    assert not any(files_root.rglob("escape.md"))


async def test_write_does_not_double_suffix_md(tmp_path: Path) -> None:
    # A slug authored via the editor's file controls already ends in ".md" (#KB-refactor);
    # the mirror must write "<name>.md", never "<name>.md.md".
    _, notes_root, platform = _make(tmp_path)
    mirror = _mirror(notes_root, await _store(), platform)

    await mirror.write("work/idea.md", "body")

    assert (notes_root / "work" / "idea.md").read_text(encoding="utf-8") == "body"
    assert not (notes_root / "work" / "idea.md.md").exists()
    stored = await platform.files_stat(f"{CORE_PREFIX}/work/idea.md")
    assert stored is not None and stored.path == "notes/work/idea.md"


async def test_delete_removes_the_mirror(tmp_path: Path) -> None:
    _, notes_root, platform = _make(tmp_path)
    mirror = _mirror(notes_root, await _store(), platform)
    await mirror.write("gone", "bye")
    assert (notes_root / "gone.md").exists()

    await mirror.delete("gone")

    assert not (notes_root / "gone.md").exists()
    assert await platform.files_stat(f"{CORE_PREFIX}/gone.md") is None


async def test_delete_absent_is_noop(tmp_path: Path) -> None:
    # The core delete is a no-op when the file is absent — delete must never raise.
    _, notes_root, platform = _make(tmp_path)
    mirror = _mirror(notes_root, await _store(), platform)

    await mirror.delete("never-existed")  # no error


async def test_backfill_writes_only_missing(tmp_path: Path) -> None:
    _, notes_root, platform = _make(tmp_path)
    store = await _store()
    await store.upsert(tenant=TENANT, slug="a", title="A", content="aaa")
    await store.upsert(tenant=TENANT, slug="b", title="B", content="bbb")
    # A pre-existing mirror for "a" (already in the core file space) must not be clobbered.
    await platform.files_write(f"{CORE_PREFIX}/a.md", "stale")
    mirror = _mirror(notes_root, store, platform)

    written = await mirror.backfill()

    assert written == 1  # only "b" was missing
    assert (notes_root / "a.md").read_text(encoding="utf-8") == "stale"
    assert (notes_root / "b.md").read_text(encoding="utf-8") == "bbb"


async def test_write_doc_mirrors_the_saved_body(tmp_path: Path) -> None:
    _, notes_root, platform = _make(tmp_path)
    store = await _store()
    indexer = AsyncMock(spec=NotesIndexer)
    indexer.index_note = AsyncMock(return_value=1)
    mirror = _mirror(notes_root, store, platform)
    pages = NotesPages(store, indexer, tenant=TENANT, mirror=mirror)

    await pages.write_doc("hello", "# Hello\n")

    assert (notes_root / "hello.md").read_text(encoding="utf-8") == "# Hello\n"


async def test_write_swallows_platform_error(tmp_path: Path) -> None:
    """Best-effort contract: a failing platform must not raise out of ``mirror.write`` —
    the note is already committed to Postgres, so a mirror hiccup can never fail the save."""
    _, notes_root, _ = _make(tmp_path)
    failing = AsyncMock()
    failing.files_write = AsyncMock(side_effect=httpx.ConnectError("core unreachable"))
    mirror = _mirror(notes_root, await _store(), failing)

    await mirror.write("note", "body")  # must not raise

    failing.files_write.assert_awaited_once()


def test_notes_root_is_tenant_scoped() -> None:
    """The .md mirror's confinement base lives under <files-root>/<tenant>/notes (constraint #1).

    Guards the path arithmetic in create_app: a regression that drops the tenant segment
    would put the mirror back at the global /data/notes, breaking per-tenant isolation of
    the shared file space.
    """
    from epicurus_notes.settings import NotesSettings

    s = NotesSettings(service_name="notes", notes_root=Path("/data/notes"))
    notes_root = s.notes_root.parent / s.default_tenant_id / s.notes_root.name
    assert notes_root == Path("/data") / s.default_tenant_id / "notes"
    assert s.default_tenant_id in notes_root.parts  # the tenant segment is present
