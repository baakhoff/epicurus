"""Mail's migration environment — the three facts the Alembic runner needs about it.

The schema of every table this module owns comes from the revisions in ``versions/``, applied
at startup by :func:`epicurus_core.db.migrations.run_migrations` (#834, #932, ADR-XXXX). This
module is what lets that runner — and ``scripts/migrate.py``, which discovers services by
globbing for the ``env.py`` beside this file — describe the service without importing its app:
no settings, no database URL, no Gmail provider, no event bus. Just the store module that
declares tables.

It lives *inside* the package rather than at ``services/mail/alembic/`` because the service
image is built with ``uv sync --no-editable``, which installs the wheel hatchling assembles
from ``src/epicurus_mail`` and nothing else. A migration directory outside that tree would
pass every test in CI and then be missing from the container at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from sqlalchemy import MetaData

# One private DeclarativeBase per store module is the house pattern, and reaching for it by its
# underscored name is right here: the migration environment is part of this service, in the
# same distribution, and giving the store a public metadata alias used by this file alone would
# be noise. Mail's local cache (ADR-0096) is the only store module in this service — five
# tables (mail_thread, mail_label, mail_sync, mail_landing, mail_category) under one
# DeclarativeBase, not two as the originating issue assumed; add a line here if a second store
# module is ever introduced.
from epicurus_mail.db import _Base as _cache_base

#: Service name — the ``services/<name>`` directory. The version table
#: (``alembic_version_mail``) and the startup advisory lock both key on it, and
#: ``scripts/migrate.py`` refuses to load an environment whose ``SERVICE`` disagrees with its
#: directory.
SERVICE: Final = "mail"

#: Where the revisions live — Alembic's ``script_location``.
SCRIPT_LOCATION: Final[Path] = Path(__file__).resolve().parent

#: Every ``MetaData`` this service owns, one per store module. Autogenerate and ``alembic
#: check`` compare the models against the database through this list, so a store module left
#: out of it is a store whose drift nothing detects.
METADATAS: Final[tuple[MetaData, ...]] = (_cache_base.metadata,)

__all__ = ["METADATAS", "SCRIPT_LOCATION", "SERVICE"]
