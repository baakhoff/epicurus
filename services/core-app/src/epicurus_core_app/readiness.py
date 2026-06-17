"""Chat readiness — what the system is doing while a turn warms up (ADR-0027).

When a chat is sent after the stack has been cold or idle, the first token can lag:
modules may still be booting and a local model may need to load into VRAM. Rather than an
opaque wait, the core reports a *readiness* snapshot — the power state, module health, and
whether the turn's model is warm — both as a queryable endpoint
(``GET /platform/v1/readiness``) and, in-band, as the ``readiness`` events that lead a
streaming turn (the web's progress bar consumes these). The probe is best-effort: a slow or
failing component never blocks a chat — it just reports as not-yet-ready and the answer
streams in regardless.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter
from pydantic import BaseModel

from epicurus_core import get_logger
from epicurus_core_app.llm.gateway import LlmGateway
from epicurus_core_app.llm.models import PowerState
from epicurus_core_app.llm.power import PowerController
from epicurus_core_app.modules import ModuleRegistry

log = get_logger("epicurus_core_app.readiness")


class ReadinessComponent(BaseModel):
    """One warming component's state, for the progress bar (ADR-0027)."""

    name: str  # "modules" | "model"
    ready: bool
    detail: str = ""  # human-readable, e.g. "3/3 healthy", "llama3.2 · warming"


class Readiness(BaseModel):
    """A point-in-time readiness snapshot for a chat turn (ADR-0027)."""

    ready: bool
    power: PowerState
    components: list[ReadinessComponent]


class ReadinessProbe:
    """Folds the power state, module registry, and LLM gateway into a readiness snapshot."""

    def __init__(
        self,
        *,
        power: PowerController,
        gateway: LlmGateway,
        registry: ModuleRegistry,
        default_tenant: str = "local",
    ) -> None:
        self._power = power
        self._gateway = gateway
        self._registry = registry
        self._default_tenant = default_tenant

    async def check(self, *, model: str | None = None, tenant_id: str | None = None) -> Readiness:
        """A resolved snapshot — probe modules + model concurrently, fold into one state."""
        tenant = tenant_id or self._default_tenant
        modules, model_state = await asyncio.gather(self._modules(), self._model(model, tenant))
        components = [modules, model_state]
        ready = self._power.state is not PowerState.PAUSED and all(c.ready for c in components)
        return Readiness(ready=ready, power=self._power.state, components=components)

    async def stream(
        self, *, model: str | None = None, tenant_id: str | None = None
    ) -> AsyncIterator[Readiness]:
        """Yield an instant *pending* snapshot, then the resolved one (ADR-0027).

        The pending frame lets the UI paint a progress bar immediately — before the module
        and model round-trips return — and the resolved frame fills in the real state.
        """
        yield Readiness(
            ready=False,
            power=self._power.state,
            components=[
                ReadinessComponent(name="modules", ready=False, detail="checking…"),
                ReadinessComponent(name="model", ready=False, detail="checking…"),
            ],
        )
        yield await self.check(model=model, tenant_id=tenant_id)

    async def _modules(self) -> ReadinessComponent:
        """Module health as a count. A down module never blocks a chat — it just shows here."""
        try:
            snaps = await self._registry.snapshot()
        except Exception as exc:  # registry trouble must not block the chat
            log.warning("module readiness probe failed", error=str(exc))
            return ReadinessComponent(name="modules", ready=True, detail="unknown")
        if not snaps:
            return ReadinessComponent(name="modules", ready=True, detail="none")
        healthy = sum(1 for s in snaps if s.status.healthy)
        total = len(snaps)
        return ReadinessComponent(
            name="modules", ready=healthy == total, detail=f"{healthy}/{total} healthy"
        )

    async def _model(self, model: str | None, tenant: str) -> ReadinessComponent:
        """Whether the turn's model is warm; hosted models report ready (no local warm-up)."""
        try:
            name, warm = await self._gateway.model_readiness(model, tenant_id=tenant)
        except Exception as exc:  # gateway trouble must not block the chat
            log.warning("model readiness probe failed", error=str(exc))
            return ReadinessComponent(name="model", ready=True, detail="unknown")
        if warm is None:
            return ReadinessComponent(name="model", ready=True, detail=f"{name} · hosted")
        return ReadinessComponent(
            name="model", ready=warm, detail=f"{name} · {'warm' if warm else 'warming'}"
        )


def create_readiness_router(probe: ReadinessProbe) -> APIRouter:
    """The queryable readiness endpoint (ADR-0027) — also the smoke-assertable contract."""
    router = APIRouter(prefix="/platform/v1", tags=["readiness"])

    @router.get("/readiness", response_model=Readiness)
    async def get_readiness(model: str | None = None) -> Readiness:
        return await probe.check(model=model)

    return router
