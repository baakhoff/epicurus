"""The permission relay: an agent's request becomes an event, the controller answers
once / always / reject, `always` is remembered per project directory, and nothing ever waits
forever — unattended sessions reject on a short timeout, attended ones on a long one."""

from __future__ import annotations

from typing import Any


async def _pending(host: Any, sid: str) -> dict[str, Any]:
    event = await host.wait_event(sid, lambda e: e["type"] == "permission.requested")
    return dict(event["data"])


async def _answer(host: Any, sid: str, request_id: str, decision: str) -> Any:
    return await host.client.post(
        f"/v1/sessions/{sid}/permissions/{request_id}", json={"decision": decision}
    )


async def test_a_request_is_an_event_and_once_allows_it(host: Any) -> None:
    sid = await host.create()
    await host.send(sid, "permission deploy")
    req = await _pending(host, sid)
    assert req["tool_call"]["title"] == "Echo wants to: deploy"
    assert [o["kind"] for o in req["options"]] == ["allow_once", "allow_always", "reject_once"]
    assert req["unattended"] is False and req["expires_at"]

    listed = (await host.client.get(f"/v1/sessions/{sid}/permissions")).json()["permissions"]
    assert [p["id"] for p in listed] == [req["request_id"]]
    assert listed[0]["status"] == "pending"

    resp = await _answer(host, sid, req["request_id"], "once")
    assert resp.status_code == 200
    assert resp.json()["decision"] == "once"
    await host.wait_message(sid, "permission: allowed (allow-once)")
    resolved = await host.wait_event(sid, lambda e: e["type"] == "permission.resolved")
    assert resolved["data"]["by"] == "controller" and resolved["data"]["option_id"] == "allow-once"
    assert (await host.client.get(f"/v1/sessions/{sid}/permissions")).json()["permissions"] == []
    every = (
        await host.client.get(f"/v1/sessions/{sid}/permissions", params={"pending": "false"})
    ).json()["permissions"]
    assert every[0]["decision"] == "once" and every[0]["resolved_by"] == "controller"

    again = await _answer(host, sid, req["request_id"], "reject")
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "permission_not_pending"


async def test_the_resolution_is_recorded_before_the_agent_acts_on_it(host: Any) -> None:
    sid = await host.create()
    await host.send(sid, "permission x")
    req = await _pending(host, sid)
    await _answer(host, sid, req["request_id"], "once")
    await host.wait_message(sid, "permission: allowed (allow-once)")
    order = [e["type"] for e in await host.events(sid)]
    resolved = order.index("permission.resolved")
    completed = order.index("agent.tool_call_update")
    assert resolved < completed


async def test_reject(host: Any) -> None:
    sid = await host.create()
    await host.send(sid, "permission rm")
    req = await _pending(host, sid)
    await _answer(host, sid, req["request_id"], "reject")
    await host.wait_message(sid, "permission: rejected (reject-once)")


async def test_always_is_remembered_for_the_project_directory(host: Any) -> None:
    sid = await host.create(cwd="proj")
    await host.send(sid, "permission build")
    req = await _pending(host, sid)
    resp = await _answer(host, sid, req["request_id"], "always")
    assert resp.status_code == 200
    await host.wait_message(sid, "permission: allowed (allow-always)")

    # Same request again in this session: answered from memory, no request raised.
    await host.send(sid, "permission build")
    await host.wait_event(
        sid,
        lambda e: e["type"] == "permission.resolved" and e["data"]["by"] == "remembered",
    )
    requested = [e for e in await host.events(sid) if e["type"] == "permission.requested"]
    assert len(requested) == 1

    # Another session on the same project directory shares the memory...
    other = await host.create(cwd="proj")
    await host.send(other, "permission build")
    await host.wait_message(other, "permission: allowed (allow-always)")
    assert "permission.requested" not in [e["type"] for e in await host.events(other)]

    # ...a different request does not match it...
    await host.send(other, "permission deploy")
    await _pending(host, other)

    # ...and a different project directory asks again.
    elsewhere = await host.create(cwd="other-proj")
    await host.send(elsewhere, "permission build")
    await _pending(host, elsewhere)


async def test_an_unattended_session_rejects_what_nobody_answers(start_host: Any) -> None:
    host = await start_host(unattended_timeout_s=0.3)
    sid = await host.create(unattended=True)
    assert (await host.session(sid))["unattended"] is True
    await host.send(sid, "permission anything")
    req = await _pending(host, sid)
    assert req["unattended"] is True
    await host.wait_message(sid, "permission: rejected (reject-once)")
    resolved = await host.wait_event(sid, lambda e: e["type"] == "permission.resolved")
    assert resolved["data"]["by"] == "timeout" and resolved["data"]["decision"] == "reject"
    late = await _answer(host, sid, req["request_id"], "once")
    assert late.status_code == 409


async def test_the_unattended_default_comes_from_the_instance(start_host: Any) -> None:
    host = await start_host(unattended=True, unattended_timeout_s=0.3)
    sid = await host.create()
    assert (await host.session(sid))["unattended"] is True
    attended = await host.create(unattended=False)
    assert (await host.session(attended))["unattended"] is False


async def test_an_attended_request_never_waits_forever(start_host: Any) -> None:
    host = await start_host(permission_timeout_s=0.4)
    sid = await host.create()
    await host.send(sid, "permission anything")
    await host.wait_message(sid, "permission: rejected (reject-once)")
    resolved = await host.wait_event(sid, lambda e: e["type"] == "permission.resolved")
    assert resolved["data"]["by"] == "timeout"


async def test_cancelling_the_turn_answers_a_pending_request_cancelled(host: Any) -> None:
    sid = await host.create()
    await host.send(sid, "permission wait")
    req = await _pending(host, sid)
    assert (await host.client.post(f"/v1/sessions/{sid}/cancel")).status_code == 202
    await host.wait_message(sid, "permission: cancelled")
    resolved = await host.wait_event(sid, lambda e: e["type"] == "permission.resolved")
    assert resolved["data"]["decision"] == "cancelled" and resolved["data"]["by"] == "cancel"
    ended = await host.wait_event(sid, lambda e: e["type"] == "turn.ended")
    assert ended["data"]["stop_reason"] == "cancelled"
    assert (await _answer(host, sid, req["request_id"], "once")).status_code == 409


async def test_stopping_a_session_cancels_its_pending_request(host: Any) -> None:
    sid = await host.create()
    await host.send(sid, "permission wait")
    req = await _pending(host, sid)
    await host.client.post(f"/v1/sessions/{sid}/stop")
    every = (
        await host.client.get(f"/v1/sessions/{sid}/permissions", params={"pending": "false"})
    ).json()["permissions"]
    assert every[0]["id"] == req["request_id"] and every[0]["decision"] == "cancelled"


async def test_answers_are_validated(host: Any) -> None:
    sid = await host.create()
    missing = await _answer(host, sid, "0" * 32, "once")
    assert missing.status_code == 404
    await host.send(sid, "permission x")
    req = await _pending(host, sid)
    bad = await _answer(host, sid, req["request_id"], "sometimes")
    assert bad.status_code == 422
    other = await host.create()
    cross = await _answer(host, other, req["request_id"], "once")
    assert cross.status_code == 404, "a request answered through another session's URL"
