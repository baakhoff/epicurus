"""Calendar's migration environment — the three facts the Alembic runner needs about it.

The schema of every table this module owns comes from the revisions in ``versions/``, applied
at startup by :func:`epicurus_core.db.migrations.run_migrations` (#834, #928, ADR-XXXX). This
module is what lets that runner — and ``scripts/migrate.py``, which discovers services by
globbing for the ``env.py`` beside this file — describe the service without importing its app:
no settings, no database URL, no event bus, no provider.

It lives *inside* the package rather than at ``services/calendar/alembic/`` because the service
image is built with ``uv sync --no-editable``, which installs the wheel hatchling assembles
from ``src/epicurus_calendar`` and nothing else. A migration directory outside that tree would
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
from epicurus_calendar.db import _Base as _events_base
from epicurus_calendar.lead_time_prefs import _LeadTimeBase as _lead_time_base
from epicurus_calendar.scheduler import _MarkerBase as _markers_base
from epicurus_calendar.sync_store import _SyncBase as _sync_base

#: Service name — the ``services/<name>`` directory. The version table
#: (``alembic_version_calendar``) and the startup advisory lock both key on it, and
#: ``scripts/migrate.py`` refuses to load an environment whose ``SERVICE`` disagrees with its
#: directory.
SERVICE: Final = "calendar"

#: Where the revisions live — Alembic's ``script_location``.
SCRIPT_LOCATION: Final[Path] = Path(__file__).resolve().parent

#: Every ``MetaData`` this service owns, one per store module: the local event store
#: (``calendar_events``), the lead-time preference (``calendar_lead_time_prefs``), the
#: lead-time scheduler's fire-once markers (``calendar_fired_markers``), and the reconcile
#: layer's sync cursor / observed-event cache / self-write ledger (``calendar_sync_state``,
#: ``calendar_synced_event``, ``calendar_self_writes``). Autogenerate and ``alembic check``
#: compare the models against the database through this list, so a store module left out of it
#: is a store whose drift nothing detects.
METADATAS: Final[tuple[MetaData, ...]] = (
    _events_base.metadata,
    _lead_time_base.metadata,
    _markers_base.metadata,
    _sync_base.metadata,
)

__all__ = ["METADATAS", "SCRIPT_LOCATION", "SERVICE"]
