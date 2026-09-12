"""Schema management for epicurus stores — migrations, and the reconcile that preceded them.

Four modules, deliberately split by what they drag in:

``epicurus_core.db`` (this file)
    Re-exports :func:`~epicurus_core.db.reconcile.ensure_columns` and nothing else, so
    ``from epicurus_core.db import ensure_columns`` keeps working for every store that has
    not adopted Alembic yet. It imports **SQLAlchemy only** — no Alembic — because a service
    still on the pre-Alembic path installs plain ``epicurus-core`` plus SQLAlchemy and must
    not break when this library gains a migration runner (ADR-XXXX).

:mod:`epicurus_core.db.migrations`
    :func:`~epicurus_core.db.migrations.run_migrations` — the one call a migrated service
    makes at startup, under a Postgres advisory lock. Imports Alembic.

:mod:`epicurus_core.db.alembic_env`
    The shared ``env.py`` body, so a service's own ``env.py`` is three lines.

:mod:`epicurus_core.db.ops`
    The two idempotent operations a *baseline* revision is rendered against, which is what
    lets one ``upgrade head`` serve an empty database and a pre-Alembic one alike.

A migrated service declares ``epicurus-core[db]`` — that extra is what puts Alembic in its
image; SQLAlchemy every store already depends on directly.
"""

from __future__ import annotations

from epicurus_core.db.reconcile import ensure_columns

__all__ = ["ensure_columns"]
