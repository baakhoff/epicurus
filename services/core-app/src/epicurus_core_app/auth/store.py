"""The two tables behind sign-in: sessions and in-flight logins (#969). Tenant-scoped.

``auth_sessions`` holds one row per signed-in browser. The browser holds an opaque 256-bit
token; the table holds only its **SHA-256**, so a database dump (or a backup that leaks) is not
a pile of live sessions. ``auth_login_states`` holds one row per sign-in that has left for the
provider and not yet come back: the ``state`` it is keyed by, and the ``nonce`` and PKCE
``code_verifier`` the callback needs. A login row is **consumed** — deleted as it is read — so a
replayed callback finds nothing, and it expires after ten minutes either way.

Neither table is tenant *data*: a session is this installation's record of a browser, and a
login row is a request in flight. Both are excluded from tenant archives (ADR-0133; see
``portability.core_data.EXCLUSIONS``) — an imported tenant signs in again.

Rows are purged opportunistically (on sign-in and on each new login), not by a scheduler:
expired rows never authenticate anyway, so a purge that runs late costs disk, not safety.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import JSON, CursorResult, DateTime, String, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "MAX_PENDING_LOGINS",
    "AuthStore",
    "LoginState",
    "SessionRecord",
    "hash_token",
]

#: Ceiling on in-flight logins per tenant. ``/login`` is reachable by anyone who can reach the
#: door, and each call writes a row; past this the oldest in-flight logins are dropped, so a
#: flood costs the flooder's own attempts rather than an unbounded table.
MAX_PENDING_LOGINS = 1000

# Column bounds. A provider can send a `name` or `email` longer than any sane one; they are
# truncated on the way in rather than failing the sign-in on a VARCHAR overflow.
_EMAIL_LEN = 320
_NAME_LEN = 255
_SUBJECT_LEN = 255
_ISSUER_LEN = 512
NEXT_PATH_LEN = 2048


class _Base(DeclarativeBase):
    pass


class _AuthSessionRow(_Base):
    """One signed-in browser. ``id`` is the SHA-256 of the cookie's token, never the token."""

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    issuer: Mapped[str] = mapped_column(String(_ISSUER_LEN))
    subject: Mapped[str] = mapped_column(String(_SUBJECT_LEN))
    email: Mapped[str | None] = mapped_column(String(_EMAIL_LEN), nullable=True)
    name: Mapped[str | None] = mapped_column(String(_NAME_LEN), nullable=True)
    groups: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class _AuthLoginStateRow(_Base):
    """One sign-in that has gone to the provider and not yet come back."""

    __tablename__ = "auth_login_states"

    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    nonce: Mapped[str] = mapped_column(String(128))
    code_verifier: Mapped[str] = mapped_column(String(128))
    next_path: Mapped[str] = mapped_column(String(NEXT_PATH_LEN))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


def hash_token(token: str) -> str:
    """The stored form of a session token: its SHA-256, hex."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    """A stored instant as an aware UTC datetime — SQLite hands ``timezone=True`` back naive."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class SessionRecord:
    """A session as the rest of the core sees it. Carries the hash, never the token."""

    id: str
    tenant: str
    issuer: str
    subject: str
    email: str | None
    name: str | None
    groups: tuple[str, ...]
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class LoginState:
    """What the callback needs from the login it completes."""

    state: str
    nonce: str
    code_verifier: str
    next_path: str
    expires_at: datetime

    def __repr__(self) -> str:  # the nonce and verifier are the flow's secrets
        return f"LoginState(next_path={self.next_path!r}, expires_at={self.expires_at!r})"


def _record(row: _AuthSessionRow) -> SessionRecord:
    return SessionRecord(
        id=row.id,
        tenant=row.tenant,
        issuer=row.issuer,
        subject=row.subject,
        email=row.email,
        name=row.name,
        groups=tuple(g for g in (row.groups or []) if isinstance(g, str)),
        created_at=_aware(row.created_at),
        last_seen_at=_aware(row.last_seen_at),
        expires_at=_aware(row.expires_at),
    )


class AuthStore:
    """Persist sessions and login states (tenant-scoped, constraint #1)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Build this store's tables from the models — the **unit-test** schema path.

        The deployed service does not call this: its schema comes from the revisions in
        :mod:`epicurus_core_app.migrations` (revision 0007 creates these two tables), applied
        at startup (#834, ADR-0138).
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    # ── login states ─────────────────────────────────────────────────────────────

    async def add_login_state(
        self,
        *,
        tenant: str,
        state: str,
        nonce: str,
        code_verifier: str,
        next_path: str,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        """Record a login leaving for the provider; purge expired ones and cap the backlog."""
        async with self._session() as session:
            await session.execute(
                delete(_AuthLoginStateRow).where(
                    _AuthLoginStateRow.tenant == tenant, _AuthLoginStateRow.expires_at <= now
                )
            )
            pending = await session.scalar(
                select(func.count())
                .select_from(_AuthLoginStateRow)
                .where(_AuthLoginStateRow.tenant == tenant)
            )
            excess = int(pending or 0) - MAX_PENDING_LOGINS + 1
            if excess > 0:
                oldest = (
                    select(_AuthLoginStateRow.state)
                    .where(_AuthLoginStateRow.tenant == tenant)
                    .order_by(_AuthLoginStateRow.created_at)
                    .limit(excess)
                )
                await session.execute(
                    delete(_AuthLoginStateRow).where(
                        _AuthLoginStateRow.tenant == tenant,
                        _AuthLoginStateRow.state.in_(oldest.scalar_subquery()),
                    )
                )
            session.add(
                _AuthLoginStateRow(
                    state=state,
                    tenant=tenant,
                    nonce=nonce,
                    code_verifier=code_verifier,
                    next_path=next_path,
                    created_at=now,
                    expires_at=expires_at,
                )
            )
            await session.commit()

    async def take_login_state(self, *, tenant: str, state: str) -> LoginState | None:
        """Return and **delete** the login keyed by *state*; ``None`` if there is none.

        Consumed on read, and race-safe about it: of two callbacks carrying the same state,
        only the one whose ``DELETE`` removed the row gets it back. Expiry is the caller's to
        judge (it holds the clock); an expired row is still removed here, which is the point.
        """
        async with self._session() as session:
            row = await session.scalar(
                select(_AuthLoginStateRow).where(
                    _AuthLoginStateRow.tenant == tenant, _AuthLoginStateRow.state == state
                )
            )
            if row is None:
                return None
            taken = LoginState(
                state=row.state,
                nonce=row.nonce,
                code_verifier=row.code_verifier,
                next_path=row.next_path,
                expires_at=_aware(row.expires_at),
            )
            result = cast(
                "CursorResult[object]",
                await session.execute(
                    delete(_AuthLoginStateRow).where(
                        _AuthLoginStateRow.tenant == tenant, _AuthLoginStateRow.state == state
                    )
                ),
            )
            await session.commit()
            return taken if result.rowcount == 1 else None

    # ── sessions ─────────────────────────────────────────────────────────────────

    async def add_session(
        self,
        *,
        token_hash: str,
        tenant: str,
        issuer: str,
        subject: str,
        email: str | None,
        name: str | None,
        groups: list[str],
        now: datetime,
        expires_at: datetime,
    ) -> SessionRecord:
        """Store a new session (by its token's hash) and purge the tenant's expired ones."""
        row = _AuthSessionRow(
            id=token_hash,
            tenant=tenant,
            issuer=issuer[:_ISSUER_LEN],
            subject=subject[:_SUBJECT_LEN],
            email=email[:_EMAIL_LEN] if email else None,
            name=name[:_NAME_LEN] if name else None,
            groups=list(groups),
            created_at=now,
            last_seen_at=now,
            expires_at=expires_at,
        )
        async with self._session() as session:
            await session.execute(
                delete(_AuthSessionRow).where(
                    _AuthSessionRow.tenant == tenant, _AuthSessionRow.expires_at <= now
                )
            )
            session.add(row)
            await session.commit()
        return _record(row)

    async def get_session(self, *, tenant: str, token_hash: str) -> SessionRecord | None:
        """The session with this token hash, expired or not — the caller judges expiry."""
        async with self._session() as session:
            row = await session.scalar(
                select(_AuthSessionRow).where(
                    _AuthSessionRow.tenant == tenant, _AuthSessionRow.id == token_hash
                )
            )
            return None if row is None else _record(row)

    async def touch_session(
        self, *, tenant: str, token_hash: str, last_seen_at: datetime, expires_at: datetime
    ) -> bool:
        """Slide a session's expiry forward; ``False`` if it no longer exists."""
        async with self._session() as session:
            result = cast(
                "CursorResult[object]",
                await session.execute(
                    update(_AuthSessionRow)
                    .where(_AuthSessionRow.tenant == tenant, _AuthSessionRow.id == token_hash)
                    .values(last_seen_at=last_seen_at, expires_at=expires_at)
                ),
            )
            await session.commit()
            return result.rowcount == 1

    async def delete_session(self, *, tenant: str, token_hash: str) -> bool:
        """Remove a session (sign-out); ``False`` if there was none."""
        async with self._session() as session:
            result = cast(
                "CursorResult[object]",
                await session.execute(
                    delete(_AuthSessionRow).where(
                        _AuthSessionRow.tenant == tenant, _AuthSessionRow.id == token_hash
                    )
                ),
            )
            await session.commit()
            return result.rowcount == 1

    async def purge_expired(self, *, tenant: str, now: datetime) -> int:
        """Delete the tenant's expired sessions and login states; returns rows removed."""
        removed = 0
        async with self._session() as session:
            for table in (_AuthSessionRow, _AuthLoginStateRow):
                result = cast(
                    "CursorResult[object]",
                    await session.execute(
                        delete(table).where(table.tenant == tenant, table.expires_at <= now)
                    ),
                )
                removed += result.rowcount
            await session.commit()
        return removed
