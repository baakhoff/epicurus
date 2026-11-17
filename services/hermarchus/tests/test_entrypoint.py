"""The real entrypoint (``python -m epicurus_hermarchus``), run as a process.

* An agent runs as the service's own uid. What that uid could read of the *service process*
  — ``/proc/<pid>/environ`` with the bearer token in it — is closed by making the process
  non-dumpable at start (``sandbox.harden_process``, called by the entrypoint).
* SIGTERM with a controller's following stream open still stops every agent and marks its
  session interrupted (uvicorn would otherwise wait for the stream forever).
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest

from epicurus_hermarchus.sandbox import harden_process

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc")

TOKEN = "hardening-token-0123456789abcdef"

# A minimal ACP agent that, on its first prompt, tries to read its parent's environment and
# reports what it found as an agent message.
SNOOP_AGENT = textwrap.dedent(
    """
    import json, os, sys
    def send(m):
        sys.stdout.write(json.dumps(m) + "\\n"); sys.stdout.flush()
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("method") == "initialize":
            send({"jsonrpc": "2.0", "id": m["id"], "result": {"protocolVersion": 1}})
        elif m.get("method") == "session/new":
            send({"jsonrpc": "2.0", "id": m["id"], "result": {"sessionId": "s"}})
        elif m.get("method") == "session/prompt":
            try:
                with open(f"/proc/{os.getppid()}/environ", "rb") as f:
                    text = "read:" + f.read().decode("utf-8", "replace")
            except OSError as exc:
                text = f"blocked:{type(exc).__name__}"
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s",
                  "update": {"sessionUpdate": "agent_message_chunk",
                             "content": {"type": "text", "text": text}}}})
            send({"jsonrpc": "2.0", "id": m["id"], "result": {"stopReason": "end_turn"}})
    """
)


def _child_reads_parent_environ(harden: bool) -> str:
    script = textwrap.dedent(
        f"""
        import os, subprocess, sys
        from epicurus_hermarchus.sandbox import harden_process
        if {harden!r}:
            assert harden_process()
        probe = "import os;open(f'/proc/{{os.getppid()}}/environ','rb').read();print('read')"
        r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
        print(r.stdout.strip() or "blocked")
        """
    )
    env = {**os.environ, "HERMARCHUS_TOKEN": TOKEN}
    out = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_harden_process_closes_proc_environ_to_a_same_uid_child() -> None:
    assert _child_reads_parent_environ(harden=False) == "read"  # the door, without it
    assert _child_reads_parent_environ(harden=True) == "blocked"


def test_harden_process_reports_success_on_linux() -> None:
    # Run in a child: making the test runner itself non-dumpable is harmless but needless.
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from epicurus_hermarchus.sandbox import harden_process as h;print(h())",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.stdout.strip() == "True"
    assert callable(harden_process)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _serve(tmp_path: Path, **extra: str) -> tuple[subprocess.Popen[bytes], str]:
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMARCHUS_")}
    env.update(
        HERMARCHUS_TOKEN=TOKEN,
        HERMARCHUS_PORT=str(port),
        HERMARCHUS_WORKSPACE_DIR=str(tmp_path / "ws"),
        HERMARCHUS_STATE_DIR=str(tmp_path / "state"),
        HERMARCHUS_AGENT_HOME=str(tmp_path / "home"),
        HERMARCHUS_STOP_GRACE_S="2",
        **extra,
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "epicurus_hermarchus"],
        env=env,
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while True:
        try:
            if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                return proc, base
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            raise AssertionError(proc.stderr.read() if proc.stderr else "the service exited")
        if time.monotonic() > deadline:
            proc.kill()
            raise AssertionError("the service never answered /health")
        time.sleep(0.1)


def _idle_session(client: httpx.Client, agent: str) -> str:
    sid = str(client.post("/v1/sessions", json={"agent": agent, "cwd": "x"}).json()["id"])
    deadline = time.monotonic() + 20
    while client.get(f"/v1/sessions/{sid}").json()["status"] != "idle":
        assert time.monotonic() < deadline, "the session never became idle"
        time.sleep(0.05)
    return sid


def test_an_agent_cannot_read_the_token_from_the_running_service(tmp_path: Path) -> None:
    agent = tmp_path / "snoop_agent.py"
    agent.write_text(SNOOP_AGENT, encoding="utf-8")
    kinds = tmp_path / "kinds.yaml"
    kinds.write_text(
        json.dumps(
            {"kinds": {"snoop": {"name": "snoop", "command": sys.executable, "args": [str(agent)]}}}
        ),
        encoding="utf-8",
    )
    proc, base = _serve(tmp_path, HERMARCHUS_KINDS_FILE=str(kinds))
    try:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        with httpx.Client(base_url=base, headers=headers, timeout=10) as client:
            sid = _idle_session(client, "snoop")
            client.post(f"/v1/sessions/{sid}/input", json={"text": "go"})
            text = ""
            deadline = time.monotonic() + 20
            while not text:
                assert time.monotonic() < deadline, "the agent never reported"
                body = client.get(f"/v1/sessions/{sid}/events", params={"after": 0}).text
                for line in body.splitlines():
                    if line.startswith("data: ") and '"agent.message"' in line:
                        text = json.loads(line[6:])["data"]["content"]["text"]
                time.sleep(0.05)
        assert TOKEN not in text
        assert text.startswith("blocked:"), text
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def test_sigterm_with_a_following_stream_open_still_interrupts_every_session(
    tmp_path: Path,
) -> None:
    proc, base = _serve(tmp_path)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    try:
        with httpx.Client(base_url=base, headers=headers, timeout=10) as client:
            sid = _idle_session(client, "echo")
            # A controller's live stream, held open across the SIGTERM.
            with client.stream(
                "GET",
                f"/v1/sessions/{sid}/events",
                params={"follow": "true"},
                timeout=httpx.Timeout(10, read=60),
            ) as stream:
                lines = stream.iter_lines()
                while next(lines) != "event: session.ready":
                    pass
                started = time.monotonic()
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=25)
                assert time.monotonic() - started < 20
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    db = sqlite3.connect(tmp_path / "state" / "hermarchus.sqlite3")
    try:
        status = db.execute("SELECT status FROM sessions WHERE id = ?", (sid,)).fetchone()[0]
        types = [
            r[0]
            for r in db.execute("SELECT type FROM events WHERE session_id = ? ORDER BY seq", (sid,))
        ]
    finally:
        db.close()
    assert status == "interrupted"
    assert types[-2:] == ["session.stopping", "session.interrupted"]
