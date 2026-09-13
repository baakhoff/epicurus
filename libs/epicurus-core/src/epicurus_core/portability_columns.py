"""A table read and written over its own SQLAlchemy column metadata (#918).

``calendar`` and the core's own ``core_data`` set each keep their travelling tables as plain
SQLAlchemy ``Table`` objects and read/write them **generically** — column by column, off the
model's own metadata — rather than through a domain store's API. Both docstrings give the same
reason: a column added tomorrow travels tomorrow, with no edit to the portability code, which a
bespoke per-field serializer cannot promise. Both independently discovered the same defect this
buys along with it, and independently wrote the same fix for it (#903, ADR-0133):

**A ``NULL`` the model has a Python-side default for must never travel as ``NULL``.** The
additive reconcile (:mod:`epicurus_core.db`) adds a post-release column *nullable* when the
model gives it no ``server_default`` — there is nothing to backfill a populated table with —
so a long-lived installation carries ``NULL`` in a column its model declares ``default=False``
(or any other scalar default) and every read coerces that back to the intended value. A fresh
target never went through that reconcile: its ``create_all`` made the column ``NOT NULL``, and
an explicit ``None`` written by a naive ``insert()`` bypasses the ORM default and violates the
constraint — a 500 on ``POST /import`` that takes the rest of the archive down with it. Both
modules normalise the ``NULL`` away on the way out (:meth:`PortableTable.encode`) *and* on the
way in (:meth:`PortableTable.normalize`), so an archive is portable regardless of which side of
the reconcile wrote it, and a column that still cannot be filled costs one skipped row rather
than the whole set (see either call site for that half of the contract).

Two independent copies of the same fix is exactly the shape that goes stale the next time either
one changes, so this module is the fix promoted once. It is deliberately **not** something every
portable module is expected to adopt: a module that already goes through a domain store —
``tasks``, ``notes``, ``knowledge``, ``storage`` — builds each field with its own explicit
fallback (``data.get("completed", False)``, a value never passed to the model constructor at
all when absent) and never had this defect to begin with, because it never reads a whole row off
column metadata in the first place. There is nothing there to adopt (verified against every
module's ``portability.py`` for #918; the finding is recorded in that issue's closing notes, not
repeated here).
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Boolean, Column, ColumnDefault, DateTime, LargeBinary, Table

__all__ = ["PortableTable", "table_of"]


def table_of(model: Any) -> Table:
    """A mapped class's ``__table__``, narrowed (it is typed ``FromClause``, always a Table)."""
    return cast("Table", model.__table__)


def _canonical_dt(value: datetime) -> str:
    """A timezone-canonical ISO string, so a round trip through any dialect compares equal.

    SQLite has no timezone-aware type: an aware ``datetime`` written to it reads back naive,
    so the same row would encode differently before and after a round trip and a second apply
    of an unchanged archive would report *updated* where it must report *skipped*. Every instant
    a portable table stores here is UTC, so a naive value is read as UTC and an aware one is
    converted to it — one canonical spelling on both sides of the trip.
    """
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat()


_NO_DEFAULT = object()
"""Sentinel — a column has no Python-side scalar default. ``None`` cannot say this: a
``default=None`` and a missing default are different facts, and so are ``default=False`` and no
default at all."""


def _scalar_default(column: Column[Any]) -> Any:
    """*column*'s Python-side scalar default, or :data:`_NO_DEFAULT`.

    Only a plain value counts: a callable (``default=uuid4``) or a SQL-expression default
    (``server_default=func.now()``) is evaluated by the ``insert`` itself — which already
    happens for a column a record omits entirely — and there is no value to freeze into a
    *record* here.
    """
    default = column.default
    # ``is_scalar`` is what does the work, not the ``isinstance``: ``CallableColumnDefault`` and
    # ``ColumnElementColumnDefault`` are both ``ColumnDefault`` subclasses and both carry an
    # ``arg`` (the callable, the SQL element) — it is just not a value that may be frozen into a
    # record, which is the same answer as having no default at all. A ``Sequence`` is not a
    # ``ColumnDefault``, so the ``isinstance`` catches that one.
    if not isinstance(default, ColumnDefault) or not default.is_scalar:
        return _NO_DEFAULT
    return default.arg


def _defaulted(column: Column[Any], value: Any) -> Any:
    """*value*, with a ``NULL`` in a ``NOT NULL`` column replaced by the column's own default.

    A **nullable** column is left alone: its ``NULL`` is data (a plain event's ``recurrence``,
    a pre-migration master's ``timezone``), and defaulting it would rewrite real rows. Only a
    column the model declares ``NOT NULL`` can be carrying an impossible value, and only the
    additive reconcile (ADR-0067) can have put it there.
    """
    if value is not None or column.nullable:
        return value
    default = _scalar_default(column)
    return None if default is _NO_DEFAULT else default


def _encode_value(column: Column[Any], value: Any) -> Any:
    """One column value, JSON-safe."""
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        return _canonical_dt(value) if isinstance(value, datetime) else str(value)
    if isinstance(column.type, LargeBinary):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(column.type, Boolean):
        # SQLite hands back 0/1; the target may be Postgres, where the column is a real
        # boolean. Normalising here keeps an idempotent re-apply idempotent across dialects.
        return bool(value)
    return value


def _decode_value(column: Column[Any], value: Any) -> Any:
    """The inverse of :func:`_encode_value`, back to what the column wants."""
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        return datetime.fromisoformat(str(value)) if isinstance(value, str) else value
    if isinstance(column.type, LargeBinary):
        return base64.b64decode(str(value))
    if isinstance(column.type, Boolean):
        return bool(value)
    return value


@dataclass(frozen=True, slots=True)
class PortableTable:
    """One travelling table: its record ``kind`` and the natural key the upsert matches on.

    *key* names the columns that identify a row **across installations**; an empty key means
    the table holds exactly one row per tenant, so the tenant is the key and the record id is
    just the kind. *skip* names columns that must not travel — always the surrogate primary
    key, whose value is an artefact of the source database's insert order and nothing else.

    A caller builds one of these per travelling table (``PortableTable(kind=..., table=
    table_of(Model), key=(...), skip=(...))``) and calls :meth:`encode`/:meth:`decode` on the
    way out and in, :meth:`normalize` on the way in before :meth:`decode` — see either
    ``core_data.py`` or ``calendar``'s ``portability.py`` for the shape of the surrounding
    export/import loop, which stays per-module (the natural key, the upsert, the transaction
    boundary all differ) even though the column encoding does not.
    """

    kind: str
    table: Table
    key: tuple[str, ...] = ()
    skip: tuple[str, ...] = ()

    @property
    def columns(self) -> list[Column[Any]]:
        """The columns that travel: everything but ``tenant`` and the skipped surrogates."""
        return [c for c in self.table.columns if c.name != "tenant" and c.name not in self.skip]

    def identity(self, data: Mapping[str, Any]) -> str:
        """The record's stable id — the natural key's values, or the kind for a singleton."""
        if not self.key:
            return self.kind
        return "|".join(str(data.get(name)) for name in self.key)

    def encode(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """A JSON-safe mapping of the travelling columns of *row*, nulls defaulted (#903)."""
        return {
            c.name: _encode_value(c, _defaulted(c, row[c.name]))
            for c in self.columns
            if c.name in row
        }

    def decode(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Python values for the travelling columns present in *data* (unknown keys dropped)."""
        by_name = {c.name: c for c in self.columns}
        return {
            name: _decode_value(by_name[name], value)
            for name, value in data.items()
            if name in by_name
        }

    def normalize(self, data: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
        """*data* with every fillable ``NULL`` replaced, plus the ones that could not be.

        The import-side twin of :meth:`encode`, and the reason an archive written before this
        rule existed still applies cleanly: the normalisation is what the *reader* does, not
        only what the writer did. The second half of the answer names the ``NOT NULL`` columns
        still carrying a ``NULL`` — one skipped row rather than an ``IntegrityError`` that takes
        the whole set with it.
        """
        by_name = {c.name: c for c in self.columns}
        normalized: dict[str, Any] = {}
        undefaultable: list[str] = []
        for name, value in data.items():
            column = by_name.get(name)
            if column is None:
                normalized[name] = value
                continue
            filled = _defaulted(column, value)
            if filled is None and not column.nullable:
                undefaultable.append(name)
            normalized[name] = filled
        return normalized, tuple(sorted(undefaultable))
