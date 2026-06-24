"""Mirror authored notes to the shared file space as ``.md`` (#KB-refactor, req 7).

Postgres stays the source of truth for note bodies (constraint #2); this writes a copy of
each note to ``<notes_root>/<slug>.md`` so the storage module shows notes in the Files view
— browsable and readable in the split-screen reader, alongside the knowledge base. The
Files tree is read-only, so the mirror is the only writer: an in-app save keeps it in step,
and a one-time backfill writes any pre-existing notes. Best-effort throughout — a mirror
failure never fails a save.
"""

from __future__ import annotations

from pathlib import Path

from epicurus_core import get_logger
from epicurus_notes.db import NotesStore

log = get_logger("notes.mirror")


class NotesMirror:
    """Writes the read-only ``.md`` mirror of each note into the shared file space."""

    def __init__(self, root: Path, store: NotesStore, *, tenant: str) -> None:
        self._root = root
        self._store = store
        self._tenant = tenant

    def _target(self, slug: str) -> Path | None:
        """The ``.md`` path for *slug*, confined to the notes root; None if it would escape.

        A slug is a Postgres key, not a filesystem path, and may legitimately be flat; this
        is defence-in-depth so a slug containing ``..`` (or absolute parts) can never write
        outside the notes folder.
        """
        root = self._root.resolve()
        target = (root / f"{slug}.md").resolve()
        return target if target.is_relative_to(root) else None

    async def write(self, slug: str, content: str) -> None:
        """Best-effort: write *slug*'s ``.md`` mirror. Never raises (a save must not fail)."""
        try:
            target = self._target(slug)
            if target is None:
                log.warning("note slug escapes the notes root; skipping mirror", slug=slug)
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except Exception as exc:  # the note is already saved in Postgres
            log.warning("note mirror write failed", slug=slug, error=str(exc))

    async def backfill(self) -> int:
        """Write a ``.md`` for every note that does not have one yet (the one-time copy).

        Only-if-missing: subsequent in-app saves keep each mirror current, so a backfill
        never clobbers a fresher file. Returns the number of files written.
        """
        try:
            notes = await self._store.list_all(tenant=self._tenant)
        except Exception as exc:
            log.warning("note mirror backfill: list failed", error=str(exc))
            return 0
        written = 0
        for note in notes:
            target = self._target(note.slug)
            if target is None or target.exists():
                continue
            await self.write(note.slug, note.content)
            written += 1
        if written:
            log.info("note mirror backfill complete", written=written)
        return written
