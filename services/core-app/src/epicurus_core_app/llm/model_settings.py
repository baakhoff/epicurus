"""Persisted per-model settings — context window + keep-alive, per ``(tenant, model)``.

The global ``llm_prefs.context_window`` is one knob for *every* model; this store lets the
operator tune a single model (chat **or** embedding) without touching the others — a 24B
model and a 1B model want very different context budgets. Resolution is layered: a per-model
value wins, else the global pref, else the env default (see ``LlmGateway._settings_for``).

Stored in the core's Postgres database so choices survive restarts and are consistent across
devices. Auto-created on first use via ``init`` (same pattern as ``ModulePrefsStore`` /
``LlmPrefsStore``). Both columns are nullable — ``None`` means "inherit" — and a row with
nothing set is deleted rather than kept empty.
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import Integer, String, inspect, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ModelSettings(BaseModel):
    """The operator's tuning for one model; ``None`` on a field means "inherit"."""

    # Ollama context window (num_ctx); None falls through to the global pref, then the env.
    context_window: int | None = None
    # How long the runtime keeps the model loaded after use (e.g. "5m", "30s", "0", "-1").
    # None falls through to the gateway's keep-alive default.
    keep_alive: str | None = None
    # Where the model runs: "gpu" (offload all layers), "cpu" (no offload), or None = "auto"
    # (let the runtime decide). Mapped to Ollama's num_gpu by the gateway; local models only.
    device: str | None = None

    def is_empty(self) -> bool:
        return self.context_window is None and not self.keep_alive and not self.device


class _ModelSettingsBase(DeclarativeBase):
    pass


class _ModelSettingsRow(_ModelSettingsBase):
    """One row per ``(tenant, model)`` — the operator's per-model tuning."""

    __tablename__ = "model_settings"

    tenant: Mapped[str] = mapped_column(String(63), primary_key=True)
    # The model name as the runtime reports it (e.g. "llama3.2:latest"); matched loosely
    # (bare name / family) by the gateway so a request for "llama3.2" still finds it.
    model: Mapped[str] = mapped_column(String(256), primary_key=True)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    keep_alive: Mapped[str | None] = mapped_column(String(16), nullable=True)
    device: Mapped[str | None] = mapped_column(String(8), nullable=True)


class ModelSettingsStore:
    """Read/write per-model settings for a tenant."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create the schema, then add any columns introduced after first release."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_ModelSettingsBase.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        """Idempotently add columns introduced after the table's first release.

        No migration framework (the store uses ``create_all``); columns added later (e.g.
        ``device`` for the GPU/CPU choice) are added in place with a per-dialect type so it
        is portable across Postgres and the tests' SQLite.
        """
        inspector = inspect(sync_conn)
        existing = {col["name"] for col in inspector.get_columns(_ModelSettingsRow.__tablename__)}
        for name in ("context_window", "keep_alive", "device"):
            if name not in existing:
                type_sql = _ModelSettingsRow.__table__.c[name].type.compile(
                    dialect=sync_conn.dialect
                )
                sync_conn.exec_driver_sql(
                    f"ALTER TABLE {_ModelSettingsRow.__tablename__} ADD COLUMN {name} {type_sql}"
                )

    async def get(self, tenant: str, model: str) -> ModelSettings:
        """The stored settings for one model (all-``None`` when the operator set nothing)."""
        async with self._session() as session:
            row = await session.get(_ModelSettingsRow, (tenant, model))
            if row is None:
                return ModelSettings()
            return ModelSettings(
                context_window=row.context_window, keep_alive=row.keep_alive, device=row.device
            )

    async def list(self, tenant: str) -> dict[str, ModelSettings]:
        """Every model with stored settings for ``tenant``, keyed by model name."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_ModelSettingsRow).where(_ModelSettingsRow.tenant == tenant)
            )
            return {
                row.model: ModelSettings(
                    context_window=row.context_window, keep_alive=row.keep_alive, device=row.device
                )
                for row in rows
            }

    async def set(self, tenant: str, model: str, settings: ModelSettings) -> None:
        """Upsert ``model``'s settings; an all-``None`` value removes the row (back to inherit)."""
        async with self._session() as session:
            row = await session.get(_ModelSettingsRow, (tenant, model))
            if settings.is_empty():
                if row is not None:
                    await session.delete(row)
                    await session.commit()
                return
            if row is None:
                row = _ModelSettingsRow(tenant=tenant, model=model)
                session.add(row)
            row.context_window = settings.context_window
            row.keep_alive = settings.keep_alive
            row.device = settings.device
            await session.commit()
