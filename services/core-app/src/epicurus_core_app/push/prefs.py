"""Push/center preferences (tenant-scoped) — one row, shared by #670 (push) and #671 (center).

Per-category and per-automation toggles are each a :class:`ChannelPrefs` pair — ``push``
(deliver a browser push) and ``center`` (keep a durable row in the #671 notification
center) — so an operator can, say, silence push for "chat" notices while still keeping
them in the center. Same settings-primitives shape as ``timezone_prefs``/``page_order_prefs``
(a single-row-per-tenant table, self-healing ``init()``, an unset value falls back to a
sane default) rather than a dedicated ADR of its own — see ADR-0039 for the canonical
instance of the pattern.

``automation_overrides`` exists as a seam for the (not-yet-built) automations engine's
per-automation sink config — :meth:`PushPrefsStore.set_automation_override` is a plain
store method with no HTTP route in this PR, since there is nothing to configure it from
yet; :meth:`PushPrefs.effective` already prefers it over the category default so the
engine can start calling it the moment it lands.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import time

from sqlalchemy import Boolean, String, Text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core.db import ensure_columns

__all__ = [
    "KNOWN_CATEGORIES",
    "ChannelPrefs",
    "PushPrefs",
    "PushPrefsStore",
    "is_quiet_now",
    "validate_hhmm",
]

# The category taxonomy the settings UI renders a toggle row for. Deliberately small and
# platform-owned (not module-declared, unlike ADR-0018 page archetypes): a category is just
# an opaque string key everywhere else in this module, so a future module-sourced category
# degrades to an unlabeled row rather than breaking. "system" backs the test-notification
# button (see push/routes.py) and any future core-originated notice (e.g. maintenance).
KNOWN_CATEGORIES: tuple[dict[str, str], ...] = (
    {"id": "system", "label": "System"},
    {"id": "chat", "label": "Chat & agent"},
    {"id": "mail", "label": "Mail"},
    {"id": "calendar", "label": "Calendar"},
    {"id": "tasks", "label": "Tasks"},
    {"id": "automation", "label": "Automations"},
)

_DEFAULT_QUIET_START = "22:00"
_DEFAULT_QUIET_END = "07:00"


@dataclass(frozen=True)
class ChannelPrefs:
    """Whether a category/automation delivers a push and/or lands in the notification center."""

    push: bool = True
    center: bool = True


@dataclass(frozen=True)
class PushPrefs:
    """A tenant's effective push/center preferences — an immutable value from the store."""

    categories: dict[str, ChannelPrefs] = field(default_factory=dict)
    automation_overrides: dict[str, ChannelPrefs] = field(default_factory=dict)
    quiet_hours_enabled: bool = False
    quiet_hours_start: str = _DEFAULT_QUIET_START
    quiet_hours_end: str = _DEFAULT_QUIET_END

    def effective(self, category: str, automation_id: str | None = None) -> ChannelPrefs:
        """The channel prefs that govern one notification.

        An automation override wins over its category's setting (the automations engine's
        per-sink config is more specific than the category default); an unset category
        falls back to on/on, matching the "off by exception" posture of every other
        settings-primitive default in this codebase (e.g. ``module_prefs.enabled``).
        """
        if automation_id is not None and automation_id in self.automation_overrides:
            return self.automation_overrides[automation_id]
        return self.categories.get(category, ChannelPrefs())


def validate_hhmm(value: str) -> None:
    """Raise ``ValueError`` if *value* is not a valid ``HH:MM`` 24-hour time."""
    try:
        time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid HH:MM time: {value!r}") from exc


def is_quiet_now(prefs: PushPrefs, local_now: time) -> bool:
    """Whether *local_now* (the tenant's local wall-clock time) falls in the quiet window.

    Handles a window that wraps past midnight (the default 22:00-07:00) as well as one
    that doesn't. A zero-width window (start == end) is treated as never-quiet rather than
    always-quiet — a degenerate config should not silently swallow every notification.
    """
    if not prefs.quiet_hours_enabled:
        return False
    start = time.fromisoformat(prefs.quiet_hours_start)
    end = time.fromisoformat(prefs.quiet_hours_end)
    if start == end:
        return False
    if start < end:
        return start <= local_now < end
    return local_now >= start or local_now < end


class _Base(DeclarativeBase):
    pass


class _PushPrefsRow(_Base):
    """One push/center preference row per tenant."""

    __tablename__ = "push_prefs"

    tenant: Mapped[str] = mapped_column(String(63), primary_key=True)
    categories: Mapped[str] = mapped_column(Text, default="{}")
    automation_overrides: Mapped[str] = mapped_column(Text, default="{}")
    quiet_hours_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    quiet_hours_start: Mapped[str] = mapped_column(String(5), default=_DEFAULT_QUIET_START)
    quiet_hours_end: Mapped[str] = mapped_column(String(5), default=_DEFAULT_QUIET_END)


_ADDED_COLUMNS = (
    "categories",
    "automation_overrides",
    "quiet_hours_enabled",
    "quiet_hours_start",
    "quiet_hours_end",
)


class PushPrefsStore:
    """Read/write a tenant's push/center preferences."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create the schema, then add any columns introduced after first release."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        ensure_columns(sync_conn, _PushPrefsRow.__table__, _ADDED_COLUMNS)

    async def get(self, tenant: str) -> PushPrefs:
        """Return the tenant's preferences, defaulting an unset row entirely."""
        async with self._session() as session:
            row = await session.get(_PushPrefsRow, tenant)
            if row is None:
                return PushPrefs()
            return _to_value(row)

    async def set_categories(self, tenant: str, categories: dict[str, ChannelPrefs]) -> PushPrefs:
        """Merge *categories* into the tenant's stored category prefs and return the result."""
        async with self._session() as session:
            row = await self._get_or_create(session, tenant)
            merged = _decode_channels(row.categories)
            merged.update(categories)
            row.categories = _encode_channels(merged)
            await session.commit()
            await session.refresh(row)
            return _to_value(row)

    async def set_quiet_hours(
        self, tenant: str, *, enabled: bool, start: str, end: str
    ) -> PushPrefs:
        """Replace the tenant's quiet-hours window. Raises ``ValueError`` on a bad HH:MM."""
        validate_hhmm(start)
        validate_hhmm(end)
        async with self._session() as session:
            row = await self._get_or_create(session, tenant)
            row.quiet_hours_enabled = enabled
            row.quiet_hours_start = start
            row.quiet_hours_end = end
            await session.commit()
            await session.refresh(row)
            return _to_value(row)

    async def set_automation_override(
        self, tenant: str, automation_id: str, prefs: ChannelPrefs | None
    ) -> PushPrefs:
        """Set (or, with ``prefs=None``, clear) one automation's override.

        No HTTP route calls this yet (see the module docstring) — it's the seam the
        automations engine's sink config will use once that lane lands.
        """
        async with self._session() as session:
            row = await self._get_or_create(session, tenant)
            overrides = _decode_channels(row.automation_overrides)
            if prefs is None:
                overrides.pop(automation_id, None)
            else:
                overrides[automation_id] = prefs
            row.automation_overrides = _encode_channels(overrides)
            await session.commit()
            await session.refresh(row)
            return _to_value(row)

    async def _get_or_create(self, session: AsyncSession, tenant: str) -> _PushPrefsRow:
        row = await session.get(_PushPrefsRow, tenant)
        if row is None:
            row = _PushPrefsRow(tenant=tenant)
            session.add(row)
            await session.flush()
        return row


def _encode_channels(channels: dict[str, ChannelPrefs]) -> str:
    return json.dumps({k: {"push": v.push, "center": v.center} for k, v in channels.items()})


def _decode_channels(raw: str) -> dict[str, ChannelPrefs]:
    try:
        data = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(k): ChannelPrefs(push=bool(v.get("push", True)), center=bool(v.get("center", True)))
        for k, v in data.items()
        if isinstance(v, dict)
    }


def _to_value(row: _PushPrefsRow) -> PushPrefs:
    # Every added column here carries a Python-side `default=`, not a `server_default=`, so
    # a row reconciled onto a legacy table (ensure_columns, epicurus_core.db) adds them
    # NULLable with nothing to backfill — a healed row can read back None even though the
    # model declares a default. Coerce explicitly rather than leaning on json.loads'/bool's
    # incidental falsy handling.
    return PushPrefs(
        categories=_decode_channels(row.categories or "{}"),
        automation_overrides=_decode_channels(row.automation_overrides or "{}"),
        quiet_hours_enabled=bool(row.quiet_hours_enabled),
        quiet_hours_start=row.quiet_hours_start or _DEFAULT_QUIET_START,
        quiet_hours_end=row.quiet_hours_end or _DEFAULT_QUIET_END,
    )
