"""The SQLite spool: sessions, their ordered event logs, the inbox, permission requests and
remembered permissions — every row scoped by ``tenant_id`` (the bound profile).

Writes are serialised through one in-process lock: an event's sequence number is allocated
and its row committed under it, so ``seq`` is gap-free and strictly increasing per session,
and the caller fans the event out to live streams only *after* it is durable.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    MetaData,
    Table,
    Text,
    and_,
    delete,
    event,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_hermarchus import schema

__all__ = [
    "LIVE_STATUSES",
    "Event",
    "Store",
    "metadata",
    "now_iso",
]

metadata = MetaData()

sessions = Table(
    "sessions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("agent", Text, nullable=False),
    Column("cwd", Text, nullable=False),
    Column("title", Text),
    Column("status", Text, nullable=False),
    Column("unattended", Boolean, nullable=False),
    Column("agent_session_id", Text),
    Column("last_seq", Integer, nullable=False, server_default="0"),
    Column("detail", Text),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)

events = Table(
    "events",
    metadata,
    Column("tenant_id", Text, nullable=False),
    Column("session_id", Text, primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("type", Text, nullable=False),
    Column("data", Text, nullable=False),
    Column("created_at", Text, nullable=False),
)

inbox = Table(
    "inbox",
    metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("mode", Text, nullable=False),
    Column("text", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("delivered_at", Text),
)

permission_requests = Table(
    "permission_requests",
    metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("request", Text, nullable=False),
    Column("decision", Text),
    Column("resolved_by", Text),
    Column("created_at", Text, nullable=False),
    Column("expires_at", Text, nullable=False),
    Column("resolved_at", Text),
)

permission_rules = Table(
    "permission_rules",
    metadata,
    Column("tenant_id", Text, primary_key=True),
    Column("project_dir", Text, primary_key=True),
    Column("agent", Text, primary_key=True),
    Column("tool_kind", Text, primary_key=True),
    Column("title", Text, primary_key=True),
    Column("created_at", Text, nullable=False),
)

# Not terminal: the session has (or is getting) a live agent process.
LIVE_STATUSES = ("starting", "idle", "running")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_after(seconds: float) -> str:
    moment = datetime.now(UTC) + timedelta(seconds=seconds)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Event:
    session_id: str
    seq: int
    type: str
    at: str
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "seq": self.seq,
            "type": self.type,
            "at": self.at,
            "data": self.data,
        }


def _event(row: RowMapping) -> Event:
    return Event(
        session_id=row["session_id"],
        seq=int(row["seq"]),
        type=row["type"],
        at=row["created_at"],
        data=json.loads(row["data"]),
    )


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    # Take transaction control away from the driver (pysqlite opens a transaction only
    # before DML, never before DDL), so `engine.begin()` really is atomic — the schema
    # upgrade depends on it. See SQLAlchemy's "Serializable isolation / Savepoints /
    # Transactional DDL" notes for the sqlite dialects.
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


def _sqlite_begin(conn: Connection) -> None:
    conn.exec_driver_sql("BEGIN")


def make_engine(path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    event.listen(engine.sync_engine, "connect", _sqlite_pragmas)
    event.listen(engine.sync_engine, "begin", _sqlite_begin)
    return engine


class Store:
    def __init__(self, engine: AsyncEngine, tenant_id: str) -> None:
        self.engine = engine
        self.tenant_id = tenant_id
        self._write = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path, tenant_id: str) -> Store:
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = make_engine(path)
        await schema.upgrade(engine)
        return cls(engine, tenant_id)

    async def close(self) -> None:
        await self.engine.dispose()

    # ── sessions ────────────────────────────────────────────────────────────
    async def create_session(
        self, *, agent: str, cwd: str, title: str | None, unattended: bool
    ) -> dict[str, Any]:
        sid = uuid.uuid4().hex
        stamp = now_iso()
        row = {
            "id": sid,
            "tenant_id": self.tenant_id,
            "agent": agent,
            "cwd": cwd,
            "title": title,
            "status": "starting",
            "unattended": unattended,
            "agent_session_id": None,
            "last_seq": 0,
            "detail": None,
            "created_at": stamp,
            "updated_at": stamp,
        }
        async with self._write, self.engine.begin() as conn:
            await conn.execute(insert(sessions).values(**row))
        return row

    async def update_session(self, session_id: str, **fields: Any) -> None:
        fields["updated_at"] = now_iso()
        async with self._write, self.engine.begin() as conn:
            await conn.execute(
                update(sessions)
                .where(sessions.c.id == session_id, sessions.c.tenant_id == self.tenant_id)
                .values(**fields)
            )

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(sessions).where(
                            sessions.c.id == session_id, sessions.c.tenant_id == self.tenant_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def list_sessions(self) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(sessions)
                        .where(sessions.c.tenant_id == self.tenant_id)
                        .order_by(sessions.c.created_at, sessions.c.id)
                    )
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    async def live_session_ids(self) -> list[str]:
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(sessions.c.id).where(
                        sessions.c.tenant_id == self.tenant_id,
                        sessions.c.status.in_(LIVE_STATUSES),
                    )
                )
            ).all()
        return [r[0] for r in rows]

    async def delete_session(self, session_id: str) -> None:
        async with self._write, self.engine.begin() as conn:
            for table in (events, inbox, permission_requests):
                await conn.execute(
                    delete(table).where(
                        table.c.session_id == session_id, table.c.tenant_id == self.tenant_id
                    )
                )
            await conn.execute(
                delete(sessions).where(
                    sessions.c.id == session_id, sessions.c.tenant_id == self.tenant_id
                )
            )

    # ── events ──────────────────────────────────────────────────────────────
    async def append_event(
        self, session_id: str, type_: str, data: dict[str, Any] | None = None
    ) -> Event:
        payload = data or {}
        stamp = now_iso()
        async with self._write, self.engine.begin() as conn:
            current = (
                await conn.execute(
                    select(sessions.c.last_seq).where(
                        sessions.c.id == session_id, sessions.c.tenant_id == self.tenant_id
                    )
                )
            ).scalar_one()
            seq = int(current) + 1
            await conn.execute(
                insert(events).values(
                    tenant_id=self.tenant_id,
                    session_id=session_id,
                    seq=seq,
                    type=type_,
                    data=json.dumps(payload, separators=(",", ":")),
                    created_at=stamp,
                )
            )
            await conn.execute(
                update(sessions)
                .where(sessions.c.id == session_id)
                .values(last_seq=seq, updated_at=stamp)
            )
        return Event(session_id=session_id, seq=seq, type=type_, at=stamp, data=payload)

    async def events_after(self, session_id: str, after: int, limit: int = 500) -> list[Event]:
        async with self.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(events)
                        .where(
                            events.c.session_id == session_id,
                            events.c.tenant_id == self.tenant_id,
                            events.c.seq > after,
                        )
                        .order_by(events.c.seq)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        return [_event(r) for r in rows]

    # ── inbox ───────────────────────────────────────────────────────────────
    async def admit_input(self, session_id: str, *, mode: str, text: str) -> dict[str, Any]:
        row = {
            "id": uuid.uuid4().hex,
            "tenant_id": self.tenant_id,
            "session_id": session_id,
            "mode": mode,
            "text": text,
            "status": "pending",
            "created_at": now_iso(),
            "delivered_at": None,
        }
        async with self._write, self.engine.begin() as conn:
            await conn.execute(insert(inbox).values(**row))
        return row

    async def pending_inputs(self, session_id: str) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(inbox)
                        .where(
                            inbox.c.session_id == session_id,
                            inbox.c.tenant_id == self.tenant_id,
                            inbox.c.status == "pending",
                        )
                        .order_by(inbox.c.created_at, inbox.c.id)
                    )
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    async def mark_input(self, input_id: str, status: str) -> None:
        values: dict[str, Any] = {"status": status}
        if status == "delivered":
            values["delivered_at"] = now_iso()
        async with self._write, self.engine.begin() as conn:
            await conn.execute(
                update(inbox)
                .where(inbox.c.id == input_id, inbox.c.tenant_id == self.tenant_id)
                .values(**values)
            )

    async def drop_pending_inputs(self, session_id: str) -> list[str]:
        pending = [r["id"] for r in await self.pending_inputs(session_id)]
        if pending:
            async with self._write, self.engine.begin() as conn:
                await conn.execute(
                    update(inbox).where(inbox.c.id.in_(pending)).values(status="dropped")
                )
        return pending

    # ── permission requests ─────────────────────────────────────────────────
    async def create_permission(
        self, session_id: str, request: dict[str, Any], timeout_s: float
    ) -> dict[str, Any]:
        row = {
            "id": uuid.uuid4().hex,
            "tenant_id": self.tenant_id,
            "session_id": session_id,
            "status": "pending",
            "request": json.dumps(request, separators=(",", ":")),
            "decision": None,
            "resolved_by": None,
            "created_at": now_iso(),
            "expires_at": iso_after(timeout_s),
            "resolved_at": None,
        }
        async with self._write, self.engine.begin() as conn:
            await conn.execute(insert(permission_requests).values(**row))
        return row

    async def resolve_permission(self, request_id: str, *, decision: str, by: str) -> bool:
        """Mark a pending request resolved; ``False`` if it was not pending (a lost race)."""
        async with self._write, self.engine.begin() as conn:
            result = await conn.execute(
                update(permission_requests)
                .where(
                    permission_requests.c.id == request_id,
                    permission_requests.c.tenant_id == self.tenant_id,
                    permission_requests.c.status == "pending",
                )
                .values(status="resolved", decision=decision, resolved_by=by, resolved_at=now_iso())
            )
        return bool(result.rowcount)

    async def get_permission(self, request_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(permission_requests).where(
                            permission_requests.c.id == request_id,
                            permission_requests.c.tenant_id == self.tenant_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _permission(row) if row else None

    async def permissions(self, session_id: str, *, pending_only: bool) -> list[dict[str, Any]]:
        conds = [
            permission_requests.c.session_id == session_id,
            permission_requests.c.tenant_id == self.tenant_id,
        ]
        if pending_only:
            conds.append(permission_requests.c.status == "pending")
        async with self.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(permission_requests)
                        .where(and_(*conds))
                        .order_by(permission_requests.c.created_at, permission_requests.c.id)
                    )
                )
                .mappings()
                .all()
            )
        return [_permission(r) for r in rows]

    # ── remembered permissions ──────────────────────────────────────────────
    async def remember(self, *, project_dir: str, agent: str, tool_kind: str, title: str) -> None:
        async with self._write, self.engine.begin() as conn:
            exists = (
                await conn.execute(
                    select(func.count())
                    .select_from(permission_rules)
                    .where(
                        permission_rules.c.tenant_id == self.tenant_id,
                        permission_rules.c.project_dir == project_dir,
                        permission_rules.c.agent == agent,
                        permission_rules.c.tool_kind == tool_kind,
                        permission_rules.c.title == title,
                    )
                )
            ).scalar_one()
            if not exists:
                await conn.execute(
                    insert(permission_rules).values(
                        tenant_id=self.tenant_id,
                        project_dir=project_dir,
                        agent=agent,
                        tool_kind=tool_kind,
                        title=title,
                        created_at=now_iso(),
                    )
                )

    async def is_remembered(
        self, *, project_dir: str, agent: str, tool_kind: str, title: str
    ) -> bool:
        async with self.engine.connect() as conn:
            count = (
                await conn.execute(
                    select(func.count())
                    .select_from(permission_rules)
                    .where(
                        permission_rules.c.tenant_id == self.tenant_id,
                        permission_rules.c.project_dir == project_dir,
                        permission_rules.c.agent == agent,
                        permission_rules.c.tool_kind == tool_kind,
                        permission_rules.c.title == title,
                    )
                )
            ).scalar_one()
        return bool(count)

    # ── crash reconciliation ────────────────────────────────────────────────
    async def cancel_pending_permissions(self, session_ids: Sequence[str], by: str) -> list[str]:
        if not session_ids:
            return []
        async with self._write, self.engine.begin() as conn:
            ids = [
                r[0]
                for r in (
                    await conn.execute(
                        select(permission_requests.c.id).where(
                            permission_requests.c.session_id.in_(session_ids),
                            permission_requests.c.tenant_id == self.tenant_id,
                            permission_requests.c.status == "pending",
                        )
                    )
                ).all()
            ]
            if ids:
                await conn.execute(
                    update(permission_requests)
                    .where(permission_requests.c.id.in_(ids))
                    .values(
                        status="resolved",
                        decision="cancelled",
                        resolved_by=by,
                        resolved_at=now_iso(),
                    )
                )
        return ids


def _permission(row: RowMapping) -> dict[str, Any]:
    out = dict(row)
    out["request"] = json.loads(out["request"])
    out.pop("tenant_id", None)
    return out
