"""core-app's migration environment — the three facts the Alembic runner needs about it.

The schema of every table the core owns comes from the revisions in ``versions/``, applied at
startup by :func:`epicurus_core.db.migrations.run_migrations` (#834, ADR-XXXX). This module is
what lets that runner — and ``scripts/migrate.py``, which discovers services by globbing for
the ``env.py`` beside this file — describe the service without importing its app: no settings,
no database URL, no Qdrant client, no event bus. Just the store modules that declare tables.

It lives *inside* the package rather than at ``services/core-app/alembic/`` because the service
image is built with ``uv sync --no-editable``, which installs the wheel hatchling assembles
from ``src/epicurus_core_app`` and nothing else. A migration directory outside that tree would
pass every test in CI and then be missing from the container at runtime.

**Why every store's** ``init()`` **still calls** ``create_all``. The deployed core never calls
those methods — this environment is its only schema path — but the unit tests do, because a
fresh SQLite file per test is far cheaper to build from the models than to migrate, and there
is no drift to reconcile in a file that was empty a millisecond ago. That shortcut is honest
only because the ``migrations`` CI gate re-proves, on real Postgres, that ``upgrade head`` and
``create_all`` produce the same schema; without the gate, ``init()`` would be a way to add a
column to the tests and never notice that no deployment has it. ``METADATAS`` below is the
other half of the same guarantee: a store whose base is missing from it is a store whose drift
nothing detects and whose tables no revision creates, which is why
``test_core_app_migrations.py`` counts the bases rather than trusting this list to stay
complete.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from sqlalchemy import MetaData

# One private DeclarativeBase per store module is the house pattern, and reaching for it by its
# underscored name is right here: the migration environment is part of this service, in the same
# distribution, and giving 28 stores a public metadata alias used by this file alone would be
# noise. Add a line per store module as the core grows one.
from epicurus_core_app.agent.instructions import _InstrBase as _agent_instructions_base
from epicurus_core_app.agent.pending_approvals import _Base as _pending_approvals_base
from epicurus_core_app.agent.pending_drafts import _Base as _pending_drafts_base
from epicurus_core_app.agent.playbook_review import _ProposalBase as _playbook_review_base
from epicurus_core_app.agent.playbooks import _PlaybookBase as _playbooks_base
from epicurus_core_app.agent.reflection import _ReflectionBase as _reflection_base
from epicurus_core_app.agent.session_model import _Base as _session_model_base
from epicurus_core_app.agent.suspended import _Base as _suspended_base
from epicurus_core_app.automations.review import _Base as _automation_review_base
from epicurus_core_app.automations.store import _Base as _automations_base
from epicurus_core_app.event_log import _Base as _event_log_base
from epicurus_core_app.file_index import _Base as _file_index_base
from epicurus_core_app.llm.model_settings import _ModelSettingsBase as _model_settings_base
from epicurus_core_app.llm.prefs import _PrefBase as _llm_prefs_base
from epicurus_core_app.llm.saved_models import _SavedBase as _saved_models_base
from epicurus_core_app.maintenance_history import _Base as _maintenance_history_base
from epicurus_core_app.maintenance_schedule_prefs import _Base as _maintenance_schedule_base

# Two stores hang their tables off ``memory.store.Base`` instead of declaring a base of their
# own, so importing the base alone would leave ``standing_profiles`` and
# ``memory_extraction_queue`` unregistered — a mapper only joins its metadata when its module is
# imported. Named in ``_SHARED_BASE_MODULES`` below so the imports are load-bearing rather than
# mysterious, and so removing one is a visible change.
from epicurus_core_app.memory import extraction_queue as _extraction_queue_module
from epicurus_core_app.memory import profile as _profile_module
from epicurus_core_app.memory.store import Base as _memory_base
from epicurus_core_app.module_prefs import _ModulePrefBase as _module_prefs_base
from epicurus_core_app.notifications import _Base as _notifications_base
from epicurus_core_app.page_order_prefs import _PageOrderBase as _page_order_base
from epicurus_core_app.portability.jobs import _Base as _portability_jobs_base
from epicurus_core_app.push.event_subscriptions import _Base as _event_subscriptions_base
from epicurus_core_app.push.prefs import _Base as _push_prefs_base
from epicurus_core_app.push.queue import _Base as _push_queue_base
from epicurus_core_app.push.subscriptions import _Base as _push_subscriptions_base
from epicurus_core_app.scheduled_turns import _Base as _scheduled_turns_base
from epicurus_core_app.timezone_prefs import _TzBase as _timezone_prefs_base

#: Service name — the ``services/<name>`` directory. The version table
#: (``alembic_version_core_app``) and the startup advisory lock both key on it, and
#: ``scripts/migrate.py`` refuses to load an environment whose ``SERVICE`` disagrees with its
#: directory.
SERVICE: Final = "core-app"

#: Where the revisions live — Alembic's ``script_location``.
SCRIPT_LOCATION: Final[Path] = Path(__file__).resolve().parent

#: The store modules whose tables belong to another module's base. Imported above for the side
#: effect of registering those mappers; referenced here so the import cannot read as dead.
_SHARED_BASE_MODULES: Final = (_extraction_queue_module, _profile_module)

#: Every ``MetaData`` this service owns, one per store module's ``DeclarativeBase`` — 28 of
#: them, 40 tables. Autogenerate and ``alembic check`` compare the models against the database
#: through this list, so a store module left out of it is a store whose drift nothing detects.
METADATAS: Final[tuple[MetaData, ...]] = (
    _agent_instructions_base.metadata,
    _automation_review_base.metadata,
    _automations_base.metadata,
    _event_log_base.metadata,
    _event_subscriptions_base.metadata,
    _file_index_base.metadata,
    _llm_prefs_base.metadata,
    _maintenance_history_base.metadata,
    _maintenance_schedule_base.metadata,
    _memory_base.metadata,
    _model_settings_base.metadata,
    _module_prefs_base.metadata,
    _notifications_base.metadata,
    _page_order_base.metadata,
    _pending_approvals_base.metadata,
    _pending_drafts_base.metadata,
    _playbook_review_base.metadata,
    _playbooks_base.metadata,
    _portability_jobs_base.metadata,
    _push_prefs_base.metadata,
    _push_queue_base.metadata,
    _push_subscriptions_base.metadata,
    _reflection_base.metadata,
    _saved_models_base.metadata,
    _scheduled_turns_base.metadata,
    _session_model_base.metadata,
    _suspended_base.metadata,
    _timezone_prefs_base.metadata,
)

__all__ = ["METADATAS", "SCRIPT_LOCATION", "SERVICE"]
