"""``scripts/migrate.py`` — discovery, rendering fidelity, and the invariants the gate rests on.

The script is the single entry point for every migration chore: the Taskfile and the
`migrations` CI job both call it, so a defect here is a defect in the gate. What is worth
pinning without a database:

* **Discovery is a glob, not a list.** A service lane adopts migrations by adding a
  ``migrations/`` directory and must be covered from that commit — neither this script nor the
  workflow may hold a roster to forget to update.
* **Server defaults render faithfully.** Alembic's own renderer strips the quotes from a literal
  default and compiles a function default against the connected dialect, so a baseline rendered
  on SQLite would carry SQLite's DDL into a Postgres migration. Both produce a revision that
  fails only when it runs, in production, which is the class of defect migrations exist to
  remove.
* **The baseline never ships an operation the idempotent shim cannot make safe.**
* **The gate's drift arm drops only columns the reconcile can restore exactly**, or it would
  assert the reconcile does something it has never claimed to.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def migrate() -> ModuleType:
    """Import ``scripts/migrate.py`` as a module (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location(
        "_migrate_under_test", REPO / "scripts" / "migrate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ── Discovery ─────────────────────────────────────────────────────────────────


def test_discovery_is_a_glob_over_the_tree(migrate: ModuleType) -> None:
    """Every service with a migration environment is found, by shape rather than by name."""
    services = migrate.discover()
    names = [service.name for service in services]
    assert "storage" in names, "storage is the reference service; its environment must be found"
    assert names == sorted(names), "a stable order keeps the gate's output diffable"
    for service in services:
        assert (service.script_location / "env.py").is_file()
        assert service.metadatas, f"{service.name} declares no MetaData"


def test_a_service_environment_lives_inside_its_package(migrate: ModuleType) -> None:
    """The wheel is built from ``src/<package>`` only — revisions outside it ship nowhere.

    `uv sync --no-editable --package <service>` in each Dockerfile installs the wheel hatchling
    assembles from that one directory. A ``services/<name>/alembic/`` tree would pass every
    test and every CI job and then be missing from the running container.
    """
    for service in migrate.discover():
        parts = service.script_location.relative_to(REPO).parts
        assert parts[0] == "services"
        assert parts[2] == "src", f"{service.name}'s revisions are outside the installed package"
        assert parts[3] == service.package


def test_selecting_an_unmigrated_service_names_the_ones_that_exist(migrate: ModuleType) -> None:
    with pytest.raises(migrate.MigrateError, match="not migration-managed"):
        migrate.select(["definitely-not-a-service"])


def test_every_service_has_a_baseline_and_a_reachable_head(migrate: ModuleType) -> None:
    """A migration environment with no revisions is a service whose schema nothing creates."""
    for service in migrate.discover():
        revisions = sorted(p.name for p in service.versions_dir.glob("*.py"))
        assert "0001_baseline.py" in revisions, f"{service.name} has no baseline"
        assert service.head() is not None, f"{service.name} has revisions but no head"


# ── Rendering a server default ────────────────────────────────────────────────


def _default(type_: Any, server_default: Any) -> object:
    """The ``DefaultClause`` a column carries, exactly as Alembic's renderer receives it."""
    table = sa.Table("t", sa.MetaData(), sa.Column("c", type_, server_default=server_default))
    return table.c["c"].server_default


def test_a_literal_server_default_keeps_its_quotes(migrate: ModuleType) -> None:
    """``server_default="'fs'"`` must render as SQL ``'fs'``, not as the bare token ``fs``.

    Alembic's renderer strips the outer quotes and re-reprs the result, so the revision would
    emit ``DEFAULT fs`` — a column reference on Postgres, and an error at upgrade time.
    """
    assert migrate._render_server_default(_default(sa.String(16), "'fs'")) == "sa.text(\"'fs'\")"
    assert migrate._render_server_default(_default(sa.Text, "'{}'")) == "sa.text(\"'{}'\")"


def test_a_function_server_default_renders_dialect_neutrally(migrate: ModuleType) -> None:
    """``func.now()`` must survive as itself, so SQLAlchemy compiles it per dialect.

    Alembic renders it pre-compiled against whatever dialect is connected — the baseline is
    generated against SQLite, which would freeze SQLite's ``(CURRENT_TIMESTAMP)`` into a
    migration that then runs on Postgres.
    """
    rendered = migrate._render_server_default(_default(sa.DateTime(timezone=True), sa.func.now()))
    assert rendered == "sa.func.now()"
    assert "CURRENT_TIMESTAMP" not in rendered


def test_an_unrecognised_server_default_refuses_to_render(migrate: ModuleType) -> None:
    """Guessing is the one thing this must not do — a wrong default is invisible until it runs."""
    with pytest.raises(migrate.MigrateError, match="dialect-neutrally"):
        migrate._render_server_default(_default(sa.Integer, sa.func.random()))


def test_a_column_without_a_server_default_is_left_to_alembic(migrate: ModuleType) -> None:
    assert migrate._render_server_default(None) is False


# ── The generated baseline ────────────────────────────────────────────────────


def test_the_generated_baseline_uses_only_idempotent_operations(migrate: ModuleType) -> None:
    """Rendered against ``ep.``, and every ``ep.`` name has an idempotent implementation."""
    (storage,) = migrate.select(["storage"])
    rendered = migrate._render_baseline(storage)
    assert "ep.create_table(" in rendered
    assert "op.create_table(" not in rendered, "the upgrade must not use the raw, failing op"
    assert "op.drop_table(" in rendered, "the downgrade is ordinary Alembic"
    assert "batch_alter_table" not in rendered, "batch mode would bypass the idempotent shim"
    migrate._reject_unshimmed_ops(storage, rendered)


def test_an_unshimmed_operation_is_a_hard_error(migrate: ModuleType) -> None:
    """Better a loud generator failure than an ``AttributeError`` mid-upgrade in production."""
    (storage,) = migrate.select(["storage"])
    with pytest.raises(migrate.MigrateError, match="alter_column"):
        migrate._reject_unshimmed_ops(storage, "    ep.alter_column('t', 'c')\n")


def _upgrade_body(source: str) -> str:
    """The ``upgrade()`` body of a revision file, so the comparison ignores the header."""
    _, _, rest = source.partition("def upgrade() -> None:")
    body, _, _ = rest.partition("def downgrade() -> None:")
    return body.strip()


def test_the_committed_baseline_is_what_the_generator_renders(migrate: ModuleType) -> None:
    """The checked-in baseline still matches the models it was generated from.

    Not a substitute for ``alembic check`` — that runs the revisions against a real database and
    is what the `migrations` gate does — but it catches the cheap version of the same mistake in
    milliseconds: a model edited, the baseline left alone, on a service whose baseline is still
    its only revision. Both sides go through ``ruff format`` first, since the committed file was
    formatted on the way in and the renderer's own output is not.
    """
    ruff = shutil.which("ruff")
    if ruff is None:  # pragma: no cover - ruff is a dev dependency, so it is on PATH
        pytest.skip("ruff is not on PATH")

    def formatted(source: str) -> str:
        done = subprocess.run(
            [ruff, "format", "--stdin-filename", "revision.py", "-"],
            input=source,
            capture_output=True,
            text=True,
            check=True,
        )
        return _upgrade_body(done.stdout)

    checked = 0
    for service in migrate.discover():
        if len(list(service.versions_dir.glob("*.py"))) != 1:
            continue  # past the baseline, later revisions carry the difference
        committed = (service.versions_dir / "0001_baseline.py").read_text(encoding="utf-8")
        assert formatted(committed) == formatted(migrate._render_baseline(service)), (
            f"{service.name}'s models no longer match its baseline — a model changed without a "
            f"revision; run `uv run python scripts/migrate.py check {service.name}`"
        )
        checked += 1
    assert checked, "no service is still on its baseline alone — drop this test or keep one"


def test_baseline_refuses_to_overwrite_existing_revisions(migrate: ModuleType) -> None:
    """It is written once; a second run would silently discard hand-written history."""
    import argparse

    with pytest.raises(migrate.MigrateError, match="already has revisions"):
        migrate.cmd_baseline(argparse.Namespace(service="storage"))


# ── What the gate's drift arm is allowed to drop ──────────────────────────────


def test_the_drift_arm_only_drops_what_the_reconcile_restores_exactly(
    migrate: ModuleType,
) -> None:
    """A ``NOT NULL`` column with no server default comes back *nullable* — never drop one.

    The reconcile has nothing to backfill populated rows with, so it relaxes the constraint
    (the documented limit, and #903's cause). Dropping such a column in the gate would make
    ``alembic check`` report a nullability diff and fail a gate that is working correctly. A
    column inside a primary key, unique constraint or index takes that object with it when
    dropped, which is equally beyond an additive repair.
    """
    metadata = sa.MetaData()
    table = sa.Table(
        "t",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("indexed", sa.String(8), server_default="'a'", nullable=False, index=True),
        sa.Column("unique_ish", sa.String(8), server_default="'b'", nullable=False),
        sa.Column("no_default", sa.String(8), nullable=False),
        sa.Column("nullable_no_default", sa.String(8), nullable=True),
        sa.Column("literal_default", sa.String(8), server_default="'c'", nullable=False),
        sa.Column("func_default", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("unique_ish", name="uq_t_unique_ish"),
    )
    assert migrate._reconcilable_columns(table) == ["literal_default"]


def test_storages_reconcilable_column_is_the_one_the_reconcile_added(
    migrate: ModuleType,
) -> None:
    """``storage_files.source`` — the single column storage's ``_ADDED_COLUMNS`` ever held."""
    (storage,) = migrate.select(["storage"])
    columns = [
        column
        for metadata in storage.metadatas
        for table in metadata.sorted_tables
        for column in migrate._reconcilable_columns(table)
    ]
    assert columns == ["source"]
