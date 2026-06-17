"""Unit tests for the readiness probe (ADR-0027) — gateway + registry are faked."""

from __future__ import annotations

from epicurus_core import ModuleManifest
from epicurus_core_app.llm.models import PowerState
from epicurus_core_app.llm.power import PowerController
from epicurus_core_app.modules import ModuleSnapshot, ModuleStatus
from epicurus_core_app.readiness import Readiness, ReadinessComponent, ReadinessProbe


class _FakeGateway:
    """Replays a scripted ``model_readiness`` result (or raises)."""

    def __init__(self, result: tuple[str, bool | None] | Exception) -> None:
        self._result = result

    async def model_readiness(
        self, model: str | None = None, *, tenant_id: str | None = None
    ) -> tuple[str, bool | None]:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeRegistry:
    """Returns a scripted module snapshot list (or raises)."""

    def __init__(self, snaps: list[ModuleSnapshot] | Exception) -> None:
        self._snaps = snaps

    async def snapshot(self) -> list[ModuleSnapshot]:
        if isinstance(self._snaps, Exception):
            raise self._snaps
        return self._snaps


def _snap(name: str, healthy: bool) -> ModuleSnapshot:
    return ModuleSnapshot(
        manifest=ModuleManifest(name=name, version="1.0.0"),
        status=ModuleStatus(healthy=healthy),
    )


def _probe(
    *,
    gateway: _FakeGateway,
    registry: _FakeRegistry,
    power: PowerController | None = None,
) -> ReadinessProbe:
    return ReadinessProbe(
        power=power or PowerController(),
        gateway=gateway,  # type: ignore[arg-type]
        registry=registry,  # type: ignore[arg-type]
        default_tenant="local",
    )


def _component(readiness: Readiness, name: str) -> ReadinessComponent:
    found = next((c for c in readiness.components if c.name == name), None)
    assert found is not None, f"no {name!r} component"
    return found


async def test_all_warm_is_ready() -> None:
    probe = _probe(
        gateway=_FakeGateway(("llama3.2", True)),
        registry=_FakeRegistry([_snap("calendar", True), _snap("notes", True)]),
    )
    readiness = await probe.check()
    assert readiness.ready is True
    assert readiness.power is PowerState.IDLE
    assert _component(readiness, "modules").detail == "2/2 healthy"
    assert _component(readiness, "model").ready is True


async def test_cold_local_model_blocks_ready() -> None:
    probe = _probe(
        gateway=_FakeGateway(("llama3.2", False)),
        registry=_FakeRegistry([_snap("calendar", True)]),
    )
    readiness = await probe.check()
    assert readiness.ready is False
    model = _component(readiness, "model")
    assert model.ready is False and "warming" in model.detail


async def test_hosted_model_is_always_ready() -> None:
    probe = _probe(
        gateway=_FakeGateway(("claude/claude-sonnet-4-6", None)),
        registry=_FakeRegistry([]),
    )
    readiness = await probe.check()
    model = _component(readiness, "model")
    assert model.ready is True and "hosted" in model.detail
    assert readiness.ready is True  # no modules + hosted model = ready


async def test_paused_is_never_ready() -> None:
    power = PowerController()
    power.pause()
    probe = _probe(
        gateway=_FakeGateway(("llama3.2", False)),
        registry=_FakeRegistry([_snap("calendar", True)]),
        power=power,
    )
    readiness = await probe.check()
    assert readiness.ready is False
    assert readiness.power is PowerState.PAUSED


async def test_no_modules_reports_none_and_does_not_block() -> None:
    probe = _probe(
        gateway=_FakeGateway(("llama3.2", True)),
        registry=_FakeRegistry([]),
    )
    readiness = await probe.check()
    assert _component(readiness, "modules").detail == "none"
    assert readiness.ready is True


async def test_unhealthy_module_is_reported_not_ready() -> None:
    probe = _probe(
        gateway=_FakeGateway(("llama3.2", True)),
        registry=_FakeRegistry([_snap("calendar", True), _snap("notes", False)]),
    )
    readiness = await probe.check()
    modules = _component(readiness, "modules")
    assert modules.ready is False and modules.detail == "1/2 healthy"
    assert readiness.ready is False


async def test_registry_failure_degrades_without_blocking() -> None:
    probe = _probe(
        gateway=_FakeGateway(("llama3.2", True)),
        registry=_FakeRegistry(RuntimeError("registry down")),
    )
    readiness = await probe.check()
    # A registry blow-up must not stall the chat — modules report "unknown" but ready.
    assert _component(readiness, "modules").detail == "unknown"
    assert readiness.ready is True


async def test_gateway_failure_degrades_without_blocking() -> None:
    probe = _probe(
        gateway=_FakeGateway(RuntimeError("ollama down")),
        registry=_FakeRegistry([_snap("calendar", True)]),
    )
    readiness = await probe.check()
    assert _component(readiness, "model").detail == "unknown"
    assert readiness.ready is True


async def test_stream_yields_pending_then_resolved() -> None:
    probe = _probe(
        gateway=_FakeGateway(("llama3.2", True)),
        registry=_FakeRegistry([_snap("calendar", True)]),
    )
    frames = [snap async for snap in probe.stream()]
    assert len(frames) == 2
    # The first frame is an instant "pending" snapshot so the bar can paint at once.
    assert frames[0].ready is False
    assert all(c.detail == "checking…" for c in frames[0].components)
    # The second is the resolved snapshot.
    assert frames[1].ready is True
    assert _component(frames[1], "modules").detail == "1/1 healthy"
