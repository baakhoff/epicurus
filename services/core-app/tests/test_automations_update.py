"""`PUT /platform/v1/automations/{id}` — the Automations page's save (#668).

File-backed SQLite per test (see test_automations_feed's ``_engine`` for why not
``:memory:``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_core_app.automations.routes import create_automations_router
from epicurus_core_app.automations.store import AutomationStore, KillSwitchStore

TENANT = "local"


def _engine(tmp_path: Path, name: str) -> AsyncEngine:
    return create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")


class _NoRunner:
    """The runner is unused by the routes under test."""


async def _env(tmp_path: Path) -> tuple[AsyncClient, AutomationStore]:
    store = AutomationStore(_engine(tmp_path, "automations.db"))
    await store.init()
    kill = KillSwitchStore(_engine(tmp_path, "kill.db"))
    await kill.init()
    app = FastAPI()
    app.include_router(
        create_automations_router(
            store,
            kill,
            _NoRunner(),  # type: ignore[arg-type]
            default_tenant=TENANT,
        )
    )
    client = AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    )
    return client, store


def _update_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Renamed",
        "prompt": "Say something new.",
        "autonomy": "propose",
        "schedule_trigger": {"cadence": "daily", "hour": 7},
        "model": "qwen2.5:7b",
        "sinks": ["chat", "notes"],
        "chat_mode": "per_run",
        "rate_cap_per_hour": 3,
        "digest_window_minutes": 15,
        "enabled": False,
    }
    body.update(overrides)
    return body


async def _seed(store: AutomationStore) -> str:
    from epicurus_core_app.automations.model import EventTrigger

    automation = await store.create(
        tenant=TENANT,
        name="Original",
        prompt="Say something.",
        autonomy="notify",
        source="template:echo",
        event_trigger=EventTrigger(module="echo", event_type="echo.pinged"),
        sinks=["chat"],
    )
    return automation.id


async def test_update_replaces_every_editable_field(tmp_path: Path) -> None:
    client, store = await _env(tmp_path)
    automation_id = await _seed(store)

    async with client:
        resp = await client.put(f"/platform/v1/automations/{automation_id}", json=_update_body())

    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Renamed"
    assert data["prompt"] == "Say something new."
    assert data["autonomy"] == "propose"
    assert data["event_trigger"] is None  # the trigger *type* switched
    assert data["schedule_trigger"] == {"cadence": "daily", "hour": 7, "weekday": None}
    assert data["model"] == "qwen2.5:7b"
    assert sorted(data["sinks"]) == ["chat", "notes"]
    assert data["chat_mode"] == "per_run"
    assert data["rate_cap_per_hour"] == 3
    assert data["digest_window_minutes"] == 15
    assert data["enabled"] is False
    # Provenance survives however much the row is edited (#668's template acceptance).
    assert data["source"] == "template:echo"
    # The derived allowance follows the new dial.
    assert "propose" in data["allowed_tool_classes"]


async def test_update_validates_before_writing(tmp_path: Path) -> None:
    client, store = await _env(tmp_path)
    automation_id = await _seed(store)

    async with client:
        both = _update_body()
        both["event_trigger"] = {"module": "echo", "event_type": "echo.pinged"}
        resp = await client.put(f"/platform/v1/automations/{automation_id}", json=both)
        assert resp.status_code == 400  # exactly one trigger, enforced

        resp = await client.put(
            f"/platform/v1/automations/{automation_id}",
            json=_update_body(sinks=["chat", "pager"]),
        )
        assert resp.status_code == 400  # unknown sink

        resp = await client.put(
            f"/platform/v1/automations/{automation_id}", json=_update_body(name="   ")
        )
        assert resp.status_code == 400  # blank name

    # A rejected edit left the stored row untouched.
    unchanged = await store.get(tenant=TENANT, automation_id=automation_id)
    assert unchanged is not None
    assert unchanged.name == "Original"
    assert unchanged.autonomy == "notify"


async def test_update_unknown_automation_is_404(tmp_path: Path) -> None:
    client, _ = await _env(tmp_path)
    async with client:
        resp = await client.put("/platform/v1/automations/nope", json=_update_body())
    assert resp.status_code == 404
