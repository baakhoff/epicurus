"""Schema regression tests for the knowledge note index."""

from __future__ import annotations

from sqlalchemy import BigInteger

from epicurus_knowledge.db import _StoredNote


def test_mtime_ns_is_bigint_not_int32() -> None:
    # Nanosecond epoch mtimes (~1.8e18) overflow Postgres INTEGER (int32); SQLite's
    # dynamic typing hides this in unit tests, so guard the column type explicitly.
    assert isinstance(_StoredNote.__table__.c.mtime_ns.type, BigInteger)
