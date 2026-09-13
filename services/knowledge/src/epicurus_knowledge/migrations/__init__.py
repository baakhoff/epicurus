"""Knowledge's migration environment — the three facts the Alembic runner needs about it.

The schema of every table this module owns comes from the revisions in ``versions/``, applied
at startup by :func:`epicurus_core.db.migrations.run_migrations` (#834, #931, ADR-XXXX). This
module is what lets that runner — and ``scripts/migrate.py``, which discovers services by
globbing for the ``env.py`` beside this file — describe the service without importing its app:
no settings, no database URL, no Qdrant client, no event bus. Just the store modules that
declare tables.

It lives *inside* the package rather than at ``services/knowledge/alembic/`` because the
service image is built with ``uv sync --no-editable``, which installs the wheel hatchling
assembles from ``src/epicurus_knowledge`` and nothing else. A migration directory outside that
tree would pass every test in CI and then be missing from the container at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from sqlalchemy import MetaData

# One private DeclarativeBase per store module is the house pattern, and reaching for it by its
# underscored name is right here: the migration environment is part of this service, in the
# same distribution, and giving every store a public metadata alias used by this file alone
# would be noise. Add a line per store module as the service grows one.
from epicurus_knowledge.db import _DocBase as _doc_base
from epicurus_knowledge.db import _NoteBase as _note_base
from epicurus_knowledge.db import _VersionBase as _version_base
from epicurus_knowledge.module_docs import _ModuleDocBase as _module_doc_base
from epicurus_knowledge.suggestions import _AuditBase as _audit_base
from epicurus_knowledge.suggestions import _SuggestionBase as _suggestion_base

#: Service name — the ``services/<name>`` directory. The version table
#: (``alembic_version_knowledge``) and the startup advisory lock both key on it, and
#: ``scripts/migrate.py`` refuses to load an environment whose ``SERVICE`` disagrees with its
#: directory.
SERVICE: Final = "knowledge"

#: Where the revisions live — Alembic's ``script_location``.
SCRIPT_LOCATION: Final[Path] = Path(__file__).resolve().parent

#: Every ``MetaData`` this service owns, one per store module. Autogenerate and ``alembic
#: check`` compare the models against the database through this list, so a store module left
#: out of it is a store whose drift nothing detects.
METADATAS: Final[tuple[MetaData, ...]] = (
    _note_base.metadata,
    _doc_base.metadata,
    _version_base.metadata,
    _module_doc_base.metadata,
    _suggestion_base.metadata,
    _audit_base.metadata,
)

__all__ = ["METADATAS", "SCRIPT_LOCATION", "SERVICE"]
