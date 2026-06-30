"""Mirror authored notes to the shared file space as ``.md`` (#KB-refactor, req 7).

Postgres stays the source of truth for note bodies (constraint #2); this writes a copy of
each note into the core file space at ``notes/<slug>.md`` so the storage module shows notes
in the Files view — browsable and readable in the split-screen reader, alongside the
knowledge base. The Files tree is read-only, so the mirror is the only writer: an in-app
save keeps it in step, and a one-time backfill writes any pre-existing notes.

The mirror writes through the **core file API** (``PlatformClient.files_*``, ADR-0065) — the
notes service no longer mounts ``/data`` at all. The mirror is write-only output: nothing
here ever reads it back. Best-effort throughout — a mirror failure never fails a save.
"""

from __future__ import annotations

from pathlib import Path

from epicurus_core import PlatformClient, get_logger
from epicurus_notes.db import NotesStore

log = get_logger("notes.mirror")


class NotesMirror:
    """Writes the read-only ``.md`` mirror of each note into the core file space."""

    def __init__(
        self,
        root: Path,
        store: NotesStore,
        *,
        tenant: str,
        platform: PlatformClient,
        core_prefix: str,
    ) -> None:
        # ``_root`` survives only for ``_target``'s confinement arithmetic — there are no
        # filesystem reads or writes here; the core file API owns the bytes (ADR-0065).
        self._root = root
        self._store = store
        self._tenant = tenant
        self._platform = platform
        self._core_prefix = core_prefix

    def _target(self, slug: str) -> Path | None:
        """The ``.md`` path for *slug*, confined to the notes root; None if it would escape.

        A slug is a Postgres key, not a filesystem path, and may legitimately be flat or
        contain ``/`` (folders). A slug authored through the editor's file controls already
        carries a ``.md`` suffix, so we add one only when missing — never ``note.md.md``.
        This is also defence-in-depth: a slug containing ``..`` (or absolute parts) can
        never write outside the notes folder. Pure path math — no filesystem access.
        """
        root = self._root.resolve()
        rel = slug if slug.endswith(".md") else f"{slug}.md"
        target = (root / rel).resolve()
        return target if target.is_relative_to(root) else None

    def _core_path(self, target: Path) -> str:
        """Map a confined mirror *target* to its core file path (``<prefix>/<rel>``).

        The core FileStore root is ``/data/<tenant>``; the notes mirror is the ``notes``
        subtree, so a target's path relative to the notes root, prefixed with ``notes``, is
        the core-relative path the platform API expects.
        """
        rel = target.relative_to(self._root.resolve()).as_posix()
        return f"{self._core_prefix}/{rel}"

    async def write(self, slug: str, content: str) -> None:
        """Best-effort: write *slug*'s ``.md`` mirror. Never raises (a save must not fail)."""
        target = self._target(slug)
        if target is None:
            log.warning("note slug escapes the notes root; skipping mirror", slug=slug)
            return
        try:
            # The core creates parent folders, so there is nothing to mkdir here.
            await self._platform.files_write(self._core_path(target), content)
        except Exception as exc:  # incl. httpx.HTTPError; the note is already saved in Postgres
            log.warning("note mirror write failed", slug=slug, error=str(exc))

    async def delete(self, slug: str) -> None:
        """Best-effort: remove *slug*'s ``.md`` mirror (when a note is deleted). Never raises."""
        target = self._target(slug)
        if target is None:
            return
        try:
            # The core delete is a no-op when the file is absent — no existence pre-check.
            await self._platform.files_delete(self._core_path(target))
        except Exception as exc:  # incl. httpx.HTTPError
            log.warning("note mirror delete failed", slug=slug, error=str(exc))

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
            if target is None:
                continue
            try:
                present = await self._platform.files_stat(self._core_path(target)) is not None
            except Exception as exc:  # incl. httpx.HTTPError
                log.warning("note mirror backfill: stat failed", slug=note.slug, error=str(exc))
                continue
            if present:
                continue
            await self.write(note.slug, note.content)
            written += 1
        if written:
            log.info("note mirror backfill complete", written=written)
        return written
