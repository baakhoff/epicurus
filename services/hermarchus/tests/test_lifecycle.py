"""Host lifecycle: a crash leaves sessions that are reconciled to interrupted on the next
start; a clean shutdown terminates every agent and marks its session interrupted."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from epicurus_hermarchus.store import Store


def _agent_pids(home: Path) -> list[int]:
    pids = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            cmd = (d / "cmdline").read_bytes()
            env = (d / "environ").read_bytes()
        except OSError:
            continue
        if b"epicurus_hermarchus.echo_agent" in cmd and f"HOME={home}".encode() in env:
            pids.append(int(d.name))
    return pids


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.exists() and ") Z " in stat.read_text())


async def test_sessions_a_crash_left_running_are_reconciled_on_start(
    start_host: Any, settings_for: Any
) -> None:
    settings = settings_for()
    store = await Store.open(settings.database_path, "local")
    row = await store.create_session(agent="echo", cwd="p", title=None, unattended=False)
    sid = row["id"]
    await store.update_session(sid, status="running")
    await store.append_event(sid, "session.created", {})
    item = await store.admit_input(sid, mode="queue", text="never delivered")
    req = await store.create_permission(sid, {"tool_call": {}, "options": []}, 60)
    await store.close()

    host = await start_host()
    session = await host.session(sid)
    assert session["status"] == "interrupted"
    assert "crash" in session["detail"]
    events = await host.events(sid)
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert [e["type"] for e in events[1:]] == [
        "permission.resolved",
        "input.dropped",
        "session.interrupted",
    ]
    assert events[1]["data"]["request_id"] == req["id"]
    assert events[1]["data"]["by"] == "host_restart"
    assert events[2]["data"]["input_id"] == item["id"]
    resp = await host.client.post(f"/v1/sessions/{sid}/input", json={"text": "x"})
    assert resp.status_code == 409
    assert (await host.client.delete(f"/v1/sessions/{sid}")).status_code == 204


async def test_shutdown_terminates_agents_and_marks_sessions_interrupted(
    start_host: Any,
) -> None:
    host = await start_host()
    sid = await host.create()
    idle = await host.create()
    await host.send(sid, "sleep 60")
    await host.wait_event(sid, lambda e: e["type"] == "agent.thought")
    pids = _agent_pids(host.settings.agent_home)
    assert len(pids) == 2
    await host.stop()
    for _ in range(50):
        if not any(_alive(p) for p in pids):
            break
        await asyncio.sleep(0.1)
    assert not any(_alive(p) for p in pids), "an agent outlived the host"

    again = await start_host()
    for s in (sid, idle):
        row = await again.session(s)
        assert row["status"] == "interrupted"
        types = [e["type"] for e in await again.events(s)]
        assert types[-2:] == ["session.stopping", "session.interrupted"]
        assert types.count("session.interrupted") == 1, "reconciled twice"
