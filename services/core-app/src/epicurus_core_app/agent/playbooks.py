"""Named playbooks — independent blocks of operator-approved guidance (ADR-0093 §3).

The base system prompt (ADR-0083, ``instructions.py``) is one monolithic string the operator
hand-edits. A **playbook** is a *named*, independently enable-able block beside it — a discovered
procedure ("for a morning briefing, check calendar before mail"), addable and removable without
growing one giant prompt. Both halves are composed into the single string ``Agent._assemble``
already reads via :meth:`AgentInstructionsStore.get_instructions` (ADR-0093 §4) — the assembly
call site never changes.

Playbooks exist because the *agent* proposes edits to them (ADR-0093 §1): the nightly reflection
pass stages a proposal, the operator approves it through the review surface, and only then does a
write land here. Nothing in this module writes on the agent's behalf — every path terminates at an
operator's Approve (``playbook_review.py``).

**Versioning** reuses the editor's snapshot-on-save shape verbatim (ADR-0046): every save
snapshots the *previous* content into ``agent_playbook_versions``, deduplicated, capped at
:data:`MAX_VERSIONS` per playbook with the oldest pruned. A human typo is trivially retyped; an
*agent-proposed* edit the operator later regrets needs an undo, which is exactly what ADR-0046
already solved for notes/knowledge — so it is reused, not reinvented.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    String,
    Text,
    delete,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Per-playbook snapshots kept, mirroring the editor version-history cap verbatim (ADR-0046) so
# a heavily-revised playbook can't grow the table without bound.
MAX_VERSIONS = 50

# The heading a playbook's content is composed under (ADR-0093 §4) — "each under a clear
# heading, so the model can attribute guidance to its source". Kept here (not in
# ``instructions.py``) because the heading is a property of what a playbook *is*.
PLAYBOOK_HEADING = "## Playbook: {name}"


@dataclass(frozen=True)
class AgentPlaybook:
    """One named block of guidance — an immutable projection of a stored row."""

    id: str
    name: str
    content: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class PlaybookVersion:
    """One snapshot of a playbook's prior content (ADR-0046).

    ``content`` is ``None`` for list rows (bodies aren't loaded when listing) and populated for a
    single fetched version; ``size`` is the snapshot's character count.
    """

    version_id: str
    playbook_id: str
    name: str
    created_at: datetime
    size: int
    content: str | None = None


class _PlaybookBase(DeclarativeBase):
    pass


class _AgentPlaybookRow(_PlaybookBase):
    """One named playbook, scoped to a tenant (constraint #1)."""

    __tablename__ = "agent_playbooks"
    # A playbook's name is its operator-facing identity, so it is unique *within* a tenant —
    # never globally (two tenants may each own a "Morning briefing").
    __table_args__ = (Index("ix_agent_playbooks_tenant_name", "tenant", "name", unique=True),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    name: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text, default="")
    # Disabled playbooks stay stored but drop out of the composed prompt (ADR-0093 §4), so the
    # operator can silence one without losing it.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class _AgentPlaybookVersionRow(_PlaybookBase):
    """One snapshot of a playbook's content (ADR-0046's shape, per playbook)."""

    __tablename__ = "agent_playbook_versions"
    __table_args__ = (Index("ix_agent_playbook_versions_tenant_playbook", "tenant", "playbook_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vid: Mapped[str] = mapped_column(String(32), index=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    playbook_id: Mapped[str] = mapped_column(String(32))
    # Snapshotted alongside the content so a version stays readable after a rename.
    name: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def _to_value(row: _AgentPlaybookRow) -> AgentPlaybook:
    return AgentPlaybook(
        id=row.id,
        name=row.name,
        content=row.content,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PlaybookStore:
    """Tenant-scoped CRUD over named playbooks, with ADR-0046 snapshot-on-save versioning."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create the schema if it does not exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_PlaybookBase.metadata.create_all)

    async def list_playbooks(
        self, tenant: str, *, enabled_only: bool = False
    ) -> list[AgentPlaybook]:
        """*tenant*'s playbooks, oldest first, then by name.

        The composition order is part of the prompt, so it must be **total and stable**: the
        primary key is a uuid, which carries no insertion order, and two playbooks created in
        the same clock tick share a ``created_at`` (SQLite's ``now()`` is second-granular).
        ``name`` — unique per tenant — breaks that tie deterministically, so the composed
        prompt never reshuffles between two reads of unchanged data.
        """
        async with self._session() as session:
            stmt = select(_AgentPlaybookRow).where(_AgentPlaybookRow.tenant == tenant)
            if enabled_only:
                stmt = stmt.where(_AgentPlaybookRow.enabled.is_(True))
            rows = await session.scalars(
                stmt.order_by(_AgentPlaybookRow.created_at, _AgentPlaybookRow.name)
            )
            return [_to_value(row) for row in rows]

    async def get(self, tenant: str, playbook_id: str) -> AgentPlaybook | None:
        """One playbook by id, or ``None``. Tenant-scoped: another tenant's id resolves to
        ``None`` rather than leaking across the boundary (constraint #1)."""
        async with self._session() as session:
            row = await session.scalar(
                select(_AgentPlaybookRow).where(
                    _AgentPlaybookRow.tenant == tenant,
                    _AgentPlaybookRow.id == playbook_id,
                )
            )
            return _to_value(row) if row is not None else None

    async def get_by_name(self, tenant: str, name: str) -> AgentPlaybook | None:
        """One playbook by its operator-facing name, or ``None`` (the review surface's lookup:
        a proposal names its target rather than carrying an opaque id)."""
        async with self._session() as session:
            row = await session.scalar(
                select(_AgentPlaybookRow).where(
                    _AgentPlaybookRow.tenant == tenant,
                    _AgentPlaybookRow.name == name,
                )
            )
            return _to_value(row) if row is not None else None

    async def create(
        self, tenant: str, *, name: str, content: str, enabled: bool = True
    ) -> AgentPlaybook:
        """Create a new named playbook and return it.

        No prior content exists, so nothing is snapshotted — the first *edit* snapshots this
        content, matching the editor's own "snapshot the previous body" rule (ADR-0046).
        """
        async with self._session() as session:
            row = _AgentPlaybookRow(
                id=uuid.uuid4().hex,
                tenant=tenant,
                name=name,
                content=content,
                enabled=enabled,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_value(row)

    async def save(self, tenant: str, playbook_id: str, *, content: str) -> AgentPlaybook | None:
        """Overwrite a playbook's content, snapshotting the **previous** body first (ADR-0046).

        Returns the updated playbook, or ``None`` if it doesn't exist (so the caller can 404).
        A save that changes nothing records no version — the editor's own "an auto-save that
        didn't change anything must not pile up duplicates" rule.

        **Note the direction**, the one deliberate departure from the editor's version store:
        that one snapshots the content *being saved*, this one snapshots the content being
        *replaced*. The editor accumulates many operator saves, so the previous body is always
        somewhere in its history anyway; here the very first write may be an approved
        agent-authored edit against a body that was never saved through this path. Recording
        the new content would leave that original unrecoverable — precisely the undo ADR-0093 §3
        says an agent-proposed edit needs. The table, the dedup, the cap and the prune are the
        ADR-0046 shape verbatim; only *what* is snapshotted differs, and only for that reason.
        """
        async with self._session() as session:
            row = await session.scalar(
                select(_AgentPlaybookRow).where(
                    _AgentPlaybookRow.tenant == tenant,
                    _AgentPlaybookRow.id == playbook_id,
                )
            )
            if row is None:
                return None
            previous = row.content
            if previous == content:
                return _to_value(row)  # nothing changed: no write, no snapshot
            row.content = content
            row.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
        await self._snapshot(tenant, playbook_id, name=row.name, content=previous)
        return _to_value(row)

    async def set_enabled(self, tenant: str, playbook_id: str, enabled: bool) -> bool:
        """Toggle whether a playbook is composed into the prompt. True if a row was updated.

        Not a content change, so it snapshots nothing (ADR-0046 versions *content*).
        """
        async with self._session() as session:
            row = await session.scalar(
                select(_AgentPlaybookRow).where(
                    _AgentPlaybookRow.tenant == tenant,
                    _AgentPlaybookRow.id == playbook_id,
                )
            )
            if row is None:
                return False
            row.enabled = enabled
            row.updated_at = datetime.now(UTC)
            await session.commit()
            return True

    async def delete(self, tenant: str, playbook_id: str) -> bool:
        """Remove a playbook and its version history. True if a row was deleted."""
        async with self._session() as session:
            row = await session.scalar(
                select(_AgentPlaybookRow).where(
                    _AgentPlaybookRow.tenant == tenant,
                    _AgentPlaybookRow.id == playbook_id,
                )
            )
            if row is None:
                return False
            await session.delete(row)
            await session.execute(
                delete(_AgentPlaybookVersionRow).where(
                    _AgentPlaybookVersionRow.tenant == tenant,
                    _AgentPlaybookVersionRow.playbook_id == playbook_id,
                )
            )
            await session.commit()
            return True

    async def _snapshot(self, tenant: str, playbook_id: str, *, name: str, content: str) -> None:
        """Record one snapshot, deduplicated, then prune beyond :data:`MAX_VERSIONS`.

        The ADR-0046 shape verbatim: skip when the newest existing snapshot is byte-identical
        (a no-op save must not pile up duplicates), then retain only the newest snapshots for
        this playbook.
        """
        async with self._session() as session:
            newest = await session.scalar(
                select(_AgentPlaybookVersionRow.content)
                .where(
                    _AgentPlaybookVersionRow.tenant == tenant,
                    _AgentPlaybookVersionRow.playbook_id == playbook_id,
                )
                .order_by(_AgentPlaybookVersionRow.id.desc())
                .limit(1)
            )
            if newest == content:
                return
            session.add(
                _AgentPlaybookVersionRow(
                    vid=uuid.uuid4().hex,
                    tenant=tenant,
                    playbook_id=playbook_id,
                    name=name,
                    content=content,
                )
            )
            await session.commit()
            keep_ids = (
                await session.scalars(
                    select(_AgentPlaybookVersionRow.id)
                    .where(
                        _AgentPlaybookVersionRow.tenant == tenant,
                        _AgentPlaybookVersionRow.playbook_id == playbook_id,
                    )
                    .order_by(_AgentPlaybookVersionRow.id.desc())
                    .limit(MAX_VERSIONS)
                )
            ).all()
            await session.execute(
                delete(_AgentPlaybookVersionRow).where(
                    _AgentPlaybookVersionRow.tenant == tenant,
                    _AgentPlaybookVersionRow.playbook_id == playbook_id,
                    _AgentPlaybookVersionRow.id.notin_(keep_ids),
                )
            )
            await session.commit()

    async def versions(self, tenant: str, playbook_id: str) -> list[PlaybookVersion]:
        """A playbook's snapshots, newest first — bodies omitted (ADR-0046's list shape)."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_AgentPlaybookVersionRow)
                .where(
                    _AgentPlaybookVersionRow.tenant == tenant,
                    _AgentPlaybookVersionRow.playbook_id == playbook_id,
                )
                .order_by(_AgentPlaybookVersionRow.id.desc())
            )
            return [
                PlaybookVersion(
                    version_id=row.vid,
                    playbook_id=row.playbook_id,
                    name=row.name,
                    created_at=row.created_at,
                    size=len(row.content),
                )
                for row in rows
            ]

    async def version(self, tenant: str, version_id: str) -> PlaybookVersion | None:
        """One snapshot **with** its body, or ``None`` — what a rollback reads."""
        async with self._session() as session:
            row = await session.scalar(
                select(_AgentPlaybookVersionRow).where(
                    _AgentPlaybookVersionRow.tenant == tenant,
                    _AgentPlaybookVersionRow.vid == version_id,
                )
            )
            if row is None:
                return None
            return PlaybookVersion(
                version_id=row.vid,
                playbook_id=row.playbook_id,
                name=row.name,
                created_at=row.created_at,
                size=len(row.content),
                content=row.content,
            )

    async def compose(self, tenant: str) -> str:
        """Every **enabled** playbook rendered under its heading, oldest first (ADR-0093 §4).

        Returns ``""`` when the tenant has no enabled playbooks, so the caller can compose the
        base prompt alone and produce byte-identical output to the pre-playbooks behavior.
        """
        blocks = [
            f"{PLAYBOOK_HEADING.format(name=p.name)}\n\n{p.content.strip()}"
            for p in await self.list_playbooks(tenant, enabled_only=True)
            if p.content.strip()
        ]
        return "\n\n".join(blocks)
