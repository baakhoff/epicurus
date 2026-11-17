"""The ACP adapter against agents that misbehave: version negotiation, sign-in needed,
start failures and timeouts, stray stdout, methods this client does not offer."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from epicurus_hermarchus.acp import AcpAgent, AgentStartError

_PRELUDE = textwrap.dedent(
    """
    import json, sys
    def send(m):
        sys.stdout.write(json.dumps(m) + "\\n"); sys.stdout.flush()
    def recv():
        line = sys.stdin.readline()
        return json.loads(line) if line else None
    """
)


class _Host:
    async def on_update(self, update: dict[str, Any]) -> None:
        return None

    async def request_permission(
        self, tool_call: dict[str, Any], options: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {"outcome": "cancelled"}

    async def read_text_file(self, path: str, line: int | None, limit: int | None) -> str:
        return ""

    async def write_text_file(self, path: str, content: str) -> None:
        return None


def _script(tmp_path: Path, body: str) -> list[str]:
    f = tmp_path / "agent.py"
    f.write_text(_PRELUDE + textwrap.dedent(body))
    return [sys.executable, str(f)]


def _agent(tmp_path: Path, argv: list[str], **kw: Any) -> AcpAgent:
    return AcpAgent(argv=argv, cwd=tmp_path, env={"PATH": "/usr/bin:/bin"}, host=_Host(), **kw)


async def test_the_echo_agent_negotiates_version_1(tmp_path: Path) -> None:
    agent = _agent(tmp_path, [sys.executable, "-m", "epicurus_hermarchus.echo_agent"])
    try:
        info = await agent.start()
        assert info.protocol_version == 1
        assert info.agent_session_id.startswith("echo-")
        assert await agent.prompt("hello") == "end_turn"
    finally:
        await agent.close(1.0)
    assert agent.returncode is not None


async def test_an_agent_answering_another_version_is_refused(tmp_path: Path) -> None:
    agent = AcpAgent(
        argv=[sys.executable, "-m", "epicurus_hermarchus.echo_agent"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "ECHO_ACP_PROTOCOL_VERSION": "2"},
        host=_Host(),
    )
    try:
        with pytest.raises(AgentStartError, match="protocol version 2"):
            await agent.start()
    finally:
        await agent.close(1.0)


async def test_auth_required_becomes_a_sign_in_message(tmp_path: Path) -> None:
    argv = _script(
        tmp_path,
        """
        m = recv(); send({"jsonrpc": "2.0", "id": m["id"], "result": {"protocolVersion": 1,
            "authMethods": [{"id": "login", "name": "Vendor login"}]}})
        m = recv(); send({"jsonrpc": "2.0", "id": m["id"],
            "error": {"code": -32000, "message": "Authentication required"}})
        recv()
        """,
    )
    agent = _agent(tmp_path, argv)
    try:
        with pytest.raises(AgentStartError, match=r"sign-in.*Vendor login"):
            await agent.start()
    finally:
        await agent.close(1.0)


async def test_a_missing_program_is_a_start_error(tmp_path: Path) -> None:
    agent = _agent(tmp_path, [str(tmp_path / "does-not-exist")])
    with pytest.raises(AgentStartError, match="could not start"):
        await agent.start()


async def test_an_agent_that_exits_during_start(tmp_path: Path) -> None:
    agent = _agent(tmp_path, _script(tmp_path, "sys.exit(4)\n"))
    try:
        with pytest.raises(AgentStartError, match="exit code 4"):
            await agent.start()
    finally:
        await agent.close(1.0)


async def test_an_agent_that_never_answers_times_out(tmp_path: Path) -> None:
    agent = _agent(
        tmp_path, _script(tmp_path, "import time; time.sleep(60)\n"), start_timeout_s=0.5
    )
    try:
        with pytest.raises(AgentStartError, match="did not finish starting"):
            await agent.start()
    finally:
        await agent.close(0.5)
    assert agent.returncode is not None, "the hung agent was not killed"


async def test_stray_stdout_is_skipped_and_unknown_methods_are_refused(tmp_path: Path) -> None:
    argv = _script(
        tmp_path,
        """
        print("a log line on stdout", flush=True)
        m = recv(); send({"jsonrpc": "2.0", "id": m["id"], "result": {"protocolVersion": 1}})
        m = recv()
        send({"jsonrpc": "2.0", "id": 900, "method": "terminal/create", "params": {}})
        reply = recv()
        send({"jsonrpc": "2.0", "id": m["id"],
              "result": {"sessionId": "code" + str(reply["error"]["code"])}})
        recv()
        """,
    )
    agent = _agent(tmp_path, argv)
    try:
        info = await agent.start()
        assert info.agent_session_id == "code-32601"
    finally:
        await agent.close(1.0)
