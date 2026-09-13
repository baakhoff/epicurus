"""Notes' migration environment — the three facts the Alembic runner needs about it.

The schema of every table this module owns comes from the revisions in ``versions/``, applied
at startup by :func:`epicurus_core.db.migrations.run_migrations` (#834, #930, ADR-XXXX). This
module is what lets that runner — and ``scripts/migrate.py``, which discovers services by
globbing for the ``env.py`` beside this file — describe the service without importing its app:
no settings, no database, no Qdrant, no object store, no event bus. Just the store modules that
declare tables.

It lives *inside* the package rather than at ``services/notes/alembic/`` because the service
image is built with ``uv sync --no-editable``, which installs the wheel hatchling assembles
from ``src/epicurus_notes`` and nothing else. A migration directory outside that tree would
pass every test in CI and then be missing from the container at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from sqlalchemy import MetaData

# One private DeclarativeBase per store module is the house pattern, and reaching for it by its
# underscored name is right here: the migration environment is part of this service, in the
# same distribution, and giving every store a public metadata alias used by this file alone
# would be noise. Add a line per store module as the service grows one.
#
# ``db.py`` declares one base shared by three tables (notes, note_folders, note_versions);
# ``suggestions.py`` declares two more, one per table, for the pending queue and its resolved
# audit trail — three DeclarativeBase objects covering the service's five tables.
from epicurus_notes.db import _Base as _notes_base
from epicurus_notes.suggestions import _NoteAuditBase as _note_audit_base
from epicurus_notes.suggestions import _NoteSuggestionBase as _note_suggestion_base

#: Service name — the ``services/<name>`` directory. The version table
#: (``alembic_version_notes``) and the startup advisory lock both key on it, and
#: ``scripts/migrate.py`` refuses to load an environment whose ``SERVICE`` disagrees with its
#: directory.
SERVICE: Final = "notes"

#: Where the revisions live — Alembic's ``script_location``.
SCRIPT_LOCATION: Final[Path] = Path(__file__).resolve().parent

#: Every ``MetaData`` this service owns, one per store module. Autogenerate and ``alembic
#: check`` compare the models against the database through this list, so a store module left
#: out of it is a store whose drift nothing detects.
METADATAS: Final[tuple[MetaData, ...]] = (
    _notes_base.metadata,
    _note_suggestion_base.metadata,
    _note_audit_base.metadata,
)

__all__ = ["METADATAS", "SCRIPT_LOCATION", "SERVICE"]
