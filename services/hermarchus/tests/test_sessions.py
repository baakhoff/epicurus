"""The controller contract end to end, against a served Hermarchus and the real echo agent
spoken to over ACP: sessions, the inbox (queue and steer), cancel, stop and delete, the
ordered event log with replay, files, limits, and failures."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx

from epicurus_hermarchus import CONTRACT_VERSION, __version__

TOKEN = "test-token-0123456789abcdef"


def _types(events: list[dict[str, Any]]) -> list[str]:
    return [e["type"] for e in events]


# ── access ─────────────────────────────────────────────────────────────────────


async def test_health_and_metrics_are_open_and_v1_needs_the_token(host: Any) -> None:
    async with httpx.AsyncClient(base_url=host.base_url) as anon:
        health = await anon.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "service": "hermarchus", "version": __version__}
        assert (await anon.get("/metrics")).status_code == 200
        for path in ("/v1/info", "/v1/sessions"):
            resp = await anon.get(path)
            assert resp.status_code == 401
            assert resp.json()["detail"]["code"] == "unauthenticated"
            assert resp.headers["www-authenticate"] == "Bearer"
        wrong = await anon.get("/v1/info", headers={"Authorization": "Bearer " + "x" * 26})
        assert wrong.status_code == 401
        basic = await anon.get("/v1/info", headers={"Authorization": f"Basic {TOKEN}"})
        assert basic.status_code == 401
        post = await anon.post("/v1/sessions", json={"agent": "echo"})
        assert post.status_code == 401


async def test_a_request_naming_another_profile_is_refused(host: Any) -> None:
    ok = await host.client.get("/v1/info", headers={"X-Epicurus-Profile": "local"})
    assert ok.status_code == 200
    for headers, params in (
        ({"X-Epicurus-Profile": "someone-else"}, None),
        ({"X-Epicurus-Tenant": "someone-else"}, None),
        ({}, {"tenant_id": "someone-else"}),
    ):
        resp = await host.client.get("/v1/sessions", headers=headers, params=params)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["code"] == "wrong_profile"
    bad = await host.client.get("/v1/sessions", headers={"X-Epicurus-Tenant": "Not Valid"})
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "invalid_profile"


async def test_info_reports_the_binding_limits_and_every_kind(host: Any) -> None:
    info = (await host.client.get("/v1/info")).json()
    assert info["service"] == "hermarchus"
    assert info["version"] == __version__
    assert info["contract_version"] == CONTRACT_VERSION
    assert info["acp_protocol_version"] == 1
    assert info["profile"] == "local"
    assert info["limits"]["max_sessions"] == 10
    assert info["limits"]["live_sessions"] == 0
    kinds = {a["kind"]: a for a in info["agents"]}
    assert kinds["echo"]["available"] is True and kinds["echo"]["reason"] is None
    for vendor in ("claude-code", "codex", "grok-build"):
        assert kinds[vendor]["available"] is False
        assert "not found" in kinds[vendor]["reason"]
        assert kinds[vendor]["verified"] is False
        assert kinds[vendor]["install"] is None
    # A kind's env values never leave the instance — only names.
    assert kinds["claude-code"]["env"] == ["CLAUDE_AGENT_ACP_PROTOCOL"]


# ── a session ──────────────────────────────────────────────────────────────────


async def test_an_echo_session_round_trips_and_its_log_is_ordered(host: Any) -> None:
    resp = await host.client.post(
        "/v1/sessions", json={"agent": "echo", "cwd": "proj", "title": "first"}
    )
    assert resp.status_code == 201
    created = resp.json()
    sid = created["id"]
    assert created["profile"] == "local"
    assert created["agent"] == "echo" and created["cwd"] == "proj" and created["title"] == "first"
    assert (host.workspace / "proj").is_dir()
    await host.wait_status(sid, "idle")

    sent = await host.send(sid, "hello there")
    assert sent["status"] == "pending" and sent["mode"] == "queue"
    await host.wait_message(sid, "echo: hello there")
    await host.wait_status(sid, "idle")

    events = await host.events(sid)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert _types(events)[:2] == ["session.created", "session.ready"]
    ready = events[1]["data"]
    assert ready["protocol_version"] == 1
    assert ready["agent_info"]["name"] == "hermarchus-echo"
    order = _types(events)
    for earlier, later in (
        ("input.admitted", "input.delivered"),
        ("input.delivered", "turn.started"),
        ("turn.started", "agent.message"),
        ("agent.message", "turn.ended"),
    ):
        assert order.index(earlier) < order.index(later)
    assert events[-1]["data"]["stop_reason"] == "end_turn"

    listing = (await host.client.get("/v1/sessions")).json()["sessions"]
    assert [s["id"] for s in listing] == [sid]
    assert listing[0]["last_seq"] == len(events)


async def test_replay_after_a_sequence_number(host: Any, sse: Any) -> None:
    sid = await host.create()
    await host.send(sid, "one")
    await host.wait_message(sid, "echo: one")
    everything = await host.events(sid)
    tail = await host.events(sid, after=3)
    assert tail == everything[3:]
    # Last-Event-ID is honoured as the resume point when `after` is absent.
    resp = await host.client.get(f"/v1/sessions/{sid}/events", headers={"Last-Event-ID": "2"})
    assert [f["data"]["seq"] for f in sse(resp.text)] == [e["seq"] for e in everything[2:]]
    frames = sse((await host.client.get(f"/v1/sessions/{sid}/events")).text)
    assert frames[0]["id"] == "1" and frames[0]["event"] == "session.created"
    beyond = await host.events(sid, after=10_000)
    assert beyond == []


async def test_a_follow_stream_delivers_live_events_and_ends_on_stop(host: Any, sse: Any) -> None:
    sid = await host.create()
    received: list[str] = []

    async def follow() -> None:
        async with host.client.stream(
            "GET", f"/v1/sessions/{sid}/events", params={"after": 0, "follow": "true"}
        ) as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            buffer = ""
            async for chunk in resp.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    received.extend(f["event"] for f in sse(block + "\n\n") if "event" in f)

    reader = asyncio.create_task(follow())
    await asyncio.sleep(0.3)
    await host.send(sid, "live")
    await host.wait_message(sid, "echo: live")
    await host.client.post(f"/v1/sessions/{sid}/stop")
    await asyncio.wait_for(reader, timeout=15)  # the stream closes after session.stopped
    assert received[0] == "session.created"
    assert "agent.message" in received
    assert received[-1] == "session.stopped"


async def test_queued_input_waits_for_the_running_turn(host: Any) -> None:
    sid = await host.create()
    await host.send(sid, "sleep 0.6")
    await host.wait_event(sid, lambda e: e["type"] == "turn.started")
    queued = await host.send(sid, "after", mode="queue")
    await host.wait_message(sid, "echo: after")
    events = await host.events(sid)
    order = _types(events)
    first_end = order.index("turn.ended")
    delivered = [
        i
        for i, e in enumerate(events)
        if e["type"] == "input.delivered" and e["data"]["input_id"] == queued["input_id"]
    ]
    assert delivered and delivered[0] > first_end
    assert events[first_end]["data"]["stop_reason"] == "end_turn"


async def test_steer_cancels_at_the_step_boundary_and_goes_ahead_of_the_queue(host: Any) -> None:
    sid = await host.create()
    await host.send(sid, "sleep 30")
    await host.wait_event(sid, lambda e: e["type"] == "agent.thought")
    queued = await host.send(sid, "queued one", mode="queue")
    steer = await host.send(sid, "steered", mode="steer")
    await host.wait_message(sid, "echo: queued one")
    events = await host.events(sid)
    cancel = next(e for e in events if e["type"] == "turn.cancel_requested")
    assert cancel["data"] == {"reason": "steer", "input_id": steer["input_id"]}
    ended = [e for e in events if e["type"] == "turn.ended"]
    assert ended[0]["data"]["stop_reason"] == "cancelled"
    delivered = [e["data"]["input_id"] for e in events if e["type"] == "input.delivered"]
    assert delivered[1:] == [steer["input_id"], queued["input_id"]]


async def test_cancel_stops_the_running_turn_and_409s_when_idle(host: Any) -> None:
    sid = await host.create()
    idle = await host.client.post(f"/v1/sessions/{sid}/cancel")
    assert idle.status_code == 409 and idle.json()["detail"]["code"] == "no_turn_running"
    await host.send(sid, "sleep 30")
    await host.wait_event(sid, lambda e: e["type"] == "agent.thought")
    assert (await host.session(sid))["turn_running"] is True
    resp = await host.client.post(f"/v1/sessions/{sid}/cancel")
    assert resp.status_code == 202
    ended = await host.wait_event(sid, lambda e: e["type"] == "turn.ended")
    assert ended["data"]["stop_reason"] == "cancelled"
    await host.wait_status(sid, "idle")
    await host.send(sid, "still alive")
    await host.wait_message(sid, "echo: still alive")


async def test_stop_then_delete(host: Any) -> None:
    sid = await host.create()
    await host.send(sid, "sleep 30")
    await host.send(sid, "never delivered")
    await host.wait_event(sid, lambda e: e["type"] == "agent.thought")
    stopped = await host.client.post(f"/v1/sessions/{sid}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "stopped"
    events = await host.events(sid)
    order = _types(events)
    assert order.index("session.stopping") < order.index("session.stopped")
    assert order[-1] == "session.stopped"
    dropped = [e for e in events if e["type"] == "input.dropped"]
    assert len(dropped) == 1
    after = await host.client.post(f"/v1/sessions/{sid}/input", json={"text": "x"})
    assert after.status_code == 409 and after.json()["detail"]["code"] == "session_not_live"
    again = await host.client.post(f"/v1/sessions/{sid}/stop")
    assert again.status_code == 200
    assert (await host.client.delete(f"/v1/sessions/{sid}")).status_code == 204
    assert (await host.client.get(f"/v1/sessions/{sid}")).status_code == 404
    assert (await host.client.get(f"/v1/sessions/{sid}/events")).status_code == 404
    assert (await host.client.delete(f"/v1/sessions/{sid}")).status_code == 404


async def test_delete_of_a_live_session_stops_its_agent_first(host: Any) -> None:
    sid = await host.create()
    assert (await host.client.delete(f"/v1/sessions/{sid}")).status_code == 204
    info = (await host.client.get("/v1/info")).json()
    assert info["limits"]["live_sessions"] == 0


async def test_many_sessions_share_one_workspace_directory(host: Any) -> None:
    sids = await asyncio.gather(*(host.create(cwd="shared") for _ in range(4)))
    await asyncio.gather(*(host.send(sid, f"hi {i}") for i, sid in enumerate(sids)))
    for i, sid in enumerate(sids):
        await host.wait_message(sid, f"echo: hi {i}")
    # Each session's log is its own and starts at 1.
    for sid in sids:
        assert (await host.events(sid))[0]["seq"] == 1


# ── limits and refusals ───────────────────────────────────────────────────────


async def test_the_session_limit_is_enforced_with_a_clear_error(start_host: Any) -> None:
    host = await start_host(max_sessions=2)
    first = await host.create()
    await host.create()
    resp = await host.client.post("/v1/sessions", json={"agent": "echo"})
    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert detail["code"] == "session_limit_reached"
    assert "HERMARCHUS_MAX_SESSIONS=2" in detail["message"]
    await host.client.post(f"/v1/sessions/{first}/stop")
    await host.create()


async def test_unknown_and_unavailable_kinds_and_bad_cwds_are_refused(host: Any) -> None:
    unknown = await host.client.post("/v1/sessions", json={"agent": "nope"})
    assert unknown.status_code == 404
    unavailable = await host.client.post("/v1/sessions", json={"agent": "codex"})
    assert unavailable.status_code == 409
    assert unavailable.json()["detail"]["code"] == "agent_unavailable"
    for cwd in ("../outside", "/etc", "a/../../b"):
        resp = await host.client.post("/v1/sessions", json={"agent": "echo", "cwd": cwd})
        assert resp.status_code == 400, cwd
        assert resp.json()["detail"]["code"] == "invalid_cwd"
    extra = await host.client.post("/v1/sessions", json={"agent": "echo", "surprise": 1})
    assert extra.status_code == 422
    assert (await host.client.get("/v1/sessions")).json()["sessions"] == []


async def test_a_symlinked_cwd_that_escapes_is_refused(host: Any, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    host.workspace.mkdir(parents=True, exist_ok=True)
    (host.workspace / "escape").symlink_to(outside)
    resp = await host.client.post("/v1/sessions", json={"agent": "echo", "cwd": "escape/sub"})
    assert resp.status_code == 400
    assert not (outside / "sub").exists()


async def test_input_mode_is_validated(host: Any) -> None:
    sid = await host.create()
    resp = await host.client.post(f"/v1/sessions/{sid}/input", json={"text": "x", "mode": "now"})
    assert resp.status_code == 422
    empty = await host.client.post(f"/v1/sessions/{sid}/input", json={"text": ""})
    assert empty.status_code == 422


# ── files through the client file methods ─────────────────────────────────────


async def test_the_agent_writes_and_reads_inside_the_workspace(host: Any) -> None:
    sid = await host.create(cwd="proj")
    await host.send(sid, "write notes/a.txt hello world")
    await host.wait_message(sid, "wrote notes/a.txt")
    assert (host.workspace / "proj" / "notes" / "a.txt").read_text() == "hello world"
    events = await host.events(sid)
    write = next(e for e in events if e["type"] == "file.write")
    assert write["data"] == {"path": "proj/notes/a.txt", "bytes": 11}
    tool = next(e for e in events if e["type"] == "agent.tool_call")
    assert tool["data"]["kind"] == "edit" and tool["data"]["title"] == "Write notes/a.txt"
    await host.send(sid, "read notes/a.txt")
    await host.wait_message(sid, "read: hello world")


async def test_file_methods_cannot_leave_the_workspace(host: Any, tmp_path: Path) -> None:
    sid = await host.create(cwd="proj")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret")
    (host.workspace / "proj" / "out").symlink_to(outside)

    await host.send(sid, "write ../../escaped.txt nope")
    await host.wait_event(
        sid,
        lambda e: (
            e["type"] == "agent.message" and e["data"]["content"]["text"].startswith("write failed")
        ),
    )
    await host.send(sid, "write out/planted.txt nope")
    await host.send(sid, f"write {outside}/abs.txt nope")
    await host.send(sid, "read out/secret.txt")
    await host.wait_event(
        sid,
        lambda e: (
            e["type"] == "agent.message" and e["data"]["content"]["text"].startswith("read failed")
        ),
    )
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]
    assert not (tmp_path / "escaped.txt").exists()
    texts = [
        e["data"]["content"]["text"] for e in await host.events(sid) if e["type"] == "agent.message"
    ]
    assert sum(t.startswith("write failed") for t in texts) == 3
    assert not any("top secret" in t for t in texts)


# ── agent updates ─────────────────────────────────────────────────────────────


async def test_tool_calls_plans_and_thoughts_become_events(host: Any) -> None:
    sid = await host.create()
    await host.send(sid, "tool Look around")
    await host.wait_message(sid, "tool: done")
    await host.send(sid, "plan")
    await host.wait_message(sid, "plan: published")
    events = await host.events(sid)
    call = next(e for e in events if e["type"] == "agent.tool_call")
    assert call["data"]["title"] == "Look around" and call["data"]["status"] == "pending"
    statuses = [e["data"]["status"] for e in events if e["type"] == "agent.tool_call_update"]
    assert statuses == ["in_progress", "completed"]
    plan = next(e for e in events if e["type"] == "agent.plan")
    assert [p["status"] for p in plan["data"]["entries"]] == ["completed", "in_progress", "pending"]


# ── the child environment ─────────────────────────────────────────────────────


async def test_the_agent_process_does_not_inherit_the_service_environment(
    start_host: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("HERMARCHUS_TOKEN", TOKEN)
    monkeypatch.setenv("SOME_SERVICE_SECRET", "s3cr3t-value")
    monkeypatch.setenv("LANG", "C.UTF-8")
    host = await start_host()
    sid = await host.create()
    await host.send(sid, "env")
    msg = await host.wait_event(
        sid,
        lambda e: e["type"] == "agent.message" and e["data"]["content"]["text"].startswith("env:"),
    )
    names = msg["data"]["content"]["text"].removeprefix("env: ").split(",")
    assert "HOME" in names and "PATH" in names and "LANG" in names
    assert not [n for n in names if n.startswith("HERMARCHUS_")]
    assert "SOME_SERVICE_SECRET" not in names

    # And the process itself, not just what it chose to report.
    proc_root = Path("/proc")
    if proc_root.is_dir():
        found = False
        for pid_dir in proc_root.iterdir():
            if not pid_dir.name.isdigit():
                continue
            try:
                cmdline = (pid_dir / "cmdline").read_bytes()
                environ = (pid_dir / "environ").read_bytes()
            except OSError:
                continue
            if b"epicurus_hermarchus.echo_agent" not in cmdline:
                continue
            if f"HOME={host.settings.agent_home}".encode() in environ:
                found = True
                assert TOKEN.encode() not in environ
                assert b"s3cr3t-value" not in environ
                assert b"HERMARCHUS_" not in environ
        assert found, "the session's agent process was not found in /proc"
    assert os.environ["HERMARCHUS_TOKEN"] == TOKEN  # the service really had it


# ── failures ──────────────────────────────────────────────────────────────────


async def test_an_agent_that_crashes_marks_the_session_failed(host: Any) -> None:
    sid = await host.create()
    await host.send(sid, "exit 3")
    await host.send(sid, "never")
    failed = await host.wait_event(sid, lambda e: e["type"] == "session.failed")
    assert failed["data"]["exit_code"] == 3
    row = await host.wait_status(sid, "failed")
    assert "exited" in row["detail"]
    assert "input.dropped" in _types(await host.events(sid))
    resp = await host.client.post(f"/v1/sessions/{sid}/input", json={"text": "x"})
    assert resp.status_code == 409


async def test_a_turn_the_agent_errors_ends_with_the_error(host: Any) -> None:
    sid = await host.create()
    await host.send(sid, "fail")
    ended = await host.wait_event(sid, lambda e: e["type"] == "turn.ended")
    assert ended["data"]["stop_reason"] == "error"
    assert ended["data"]["error"] == "echo was asked to fail"
    await host.wait_status(sid, "idle")


async def test_metrics_count_sessions_and_events(host: Any) -> None:
    sid = await host.create()
    await host.send(sid, "hi")
    await host.wait_message(sid, "echo: hi")
    async with httpx.AsyncClient(base_url=host.base_url) as anon:
        text = (await anon.get("/metrics")).text
    assert "hermarchus_sessions_live 1.0" in text
    assert 'hermarchus_sessions_created_total{agent="echo"} 1.0' in text
    assert 'hermarchus_events_total{type="agent.message"}' in text
    assert sid not in text


async def test_a_kind_that_cannot_start_fails_its_session_with_the_reason(
    start_host: Any, tmp_path: Path
) -> None:
    kinds = tmp_path / "kinds.yaml"
    kinds.write_text(
        "kinds:\n"
        "  echo-v2:\n"
        f"    command: {__import__('sys').executable}\n"
        "    args: [-m, epicurus_hermarchus.echo_agent]\n"
        "    env: {ECHO_ACP_PROTOCOL_VERSION: '2'}\n"
    )
    host = await start_host(kinds_file=kinds)
    resp = await host.client.post("/v1/sessions", json={"agent": "echo-v2"})
    assert resp.status_code == 201
    sid = resp.json()["id"]
    row = await host.wait_status(sid, "failed")
    assert "protocol version 2" in row["detail"]
    failed = await host.wait_event(sid, lambda e: e["type"] == "session.failed")
    assert "this host speaks 1" in failed["data"]["reason"]
    assert (await host.client.get("/v1/info")).json()["limits"]["live_sessions"] == 0
