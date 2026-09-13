"""Alembic's entry point for this service — deliberately three lines.

Alembic requires an ``env.py`` inside every script location, and knowledge needs its own
script location because it owns its own ``versions/`` sequence. It does not need its own
opinion about how migrations are configured, so the whole body lives in
:func:`epicurus_core.db.alembic_env.run` — the per-service version table, batch rendering, the
type/server-default comparison the drift gate relies on, and the filter that keeps this
service's autogenerate from seeing the other services' tables in the shared database.

Everything this needs (the live connection, the metadata list, the version table) arrives
through ``config.attributes``, set by :func:`epicurus_core.db.migrations.alembic_config`. There
is no ``alembic.ini``; ``scripts/migrate.py`` and the startup runner are the only callers.
"""

from epicurus_core.db.alembic_env import run

run()
