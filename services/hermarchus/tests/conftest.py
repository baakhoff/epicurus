"""A real Hermarchus on a loopback port, per test.

The app is served by uvicorn (not an in-process ASGI transport) so the lifespan runs, agent
processes are real children, and server-sent-event streams are real streams.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from epicurus_hermarchus.app import create_app
from epicurus_hermarchus.settings import HermarchusSettings

TOKEN = "test-token-0123456789abcdef"


def parse_sse(text: str) -> list[dict[str, Any]]:
    """Frames of an SSE body: ``{"id", "event", "data"}`` (comments dropped)."""
    frames: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        frame: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith(":") or not line:
                continue
            key, _, value = line.partition(": ")
            frame[key] = value
        if frame:
            if "data" in frame:
                frame["data"] = json.loads(frame["data"])
            frames.append(frame)
    return frames


def make_settings(tmp_path: Path, **overrides: Any) -> HermarchusSettings:
    base: dict[str, Any] = {
        "token": TOKEN,
        "profile": "local",
        "workspace_dir": tmp_path / "workspace",
        "state_dir": tmp_path / "state",
        "agent_home": tmp_path / "home",
        "stop_grace_s": 2.0,
        "agent_start_timeout_s": 30.0,
    }
    base.update(overrides)
    return HermarchusSettings(**base).validated()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Host:
    settings: HermarchusSettings
    client: httpx.AsyncClient
    base_url: str
    server: uvicorn.Server
    task: asyncio.Task[None]
    _created: list[str] = field(default_factory=list)

    @property
    def workspace(self) -> Path:
        return self.settings.workspace_dir

    async def create(self, agent: str = "echo", cwd: str = "project", **body: Any) -> str:
        resp = await self.client.post("/v1/sessions", json={"agent": agent, "cwd": cwd, **body})
        assert resp.status_code == 201, resp.text
        sid = str(resp.json()["id"])
        await self.wait_status(sid, "idle")
        return sid

    async def session(self, sid: str) -> dict[str, Any]:
        resp = await self.client.get(f"/v1/sessions/{sid}")
        assert resp.status_code == 200, resp.text
        data: dict[str, Any] = resp.json()
        return data

    async def wait_status(self, sid: str, *statuses: str, timeout: float = 20.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        row: dict[str, Any] = {}
        while time.monotonic() < deadline:
            row = await self.session(sid)
            if row["status"] in statuses:
                return row
            await asyncio.sleep(0.05)
        raise AssertionError(f"session never reached {statuses}: {row}")

    async def send(self, sid: str, text: str, mode: str = "queue") -> dict[str, Any]:
        resp = await self.client.post(
            f"/v1/sessions/{sid}/input", json={"text": text, "mode": mode}
        )
        assert resp.status_code == 202, resp.text
        data: dict[str, Any] = resp.json()
        return data

    async def events(self, sid: str, after: int = 0) -> list[dict[str, Any]]:
        resp = await self.client.get(f"/v1/sessions/{sid}/events", params={"after": after})
        assert resp.status_code == 200, resp.text
        return [f["data"] for f in parse_sse(resp.text)]

    async def wait_event(
        self,
        sid: str,
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        seen: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            seen = await self.events(sid)
            for event in seen:
                if predicate(event):
                    return event
            await asyncio.sleep(0.05)
        raise AssertionError(f"no matching event; saw {[e['type'] for e in seen]}")

    async def wait_message(self, sid: str, text: str, timeout: float = 20.0) -> dict[str, Any]:
        return await self.wait_event(
            sid,
            lambda e: e["type"] == "agent.message" and e["data"]["content"].get("text") == text,
            timeout,
        )

    async def stop(self) -> None:
        self.server.should_exit = True
        await asyncio.wait_for(self.task, timeout=30)


HostFactory = Callable[..., Any]


@pytest.fixture
async def start_host(tmp_path: Path) -> AsyncIterator[HostFactory]:
    started: list[Host] = []

    async def _start(**overrides: Any) -> Host:
        settings = make_settings(tmp_path, **overrides)
        port = _free_port()
        config = uvicorn.Config(
            create_app(settings), host="127.0.0.1", port=port, log_level="warning", lifespan="on"
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        deadline = time.monotonic() + 20
        while not server.started:
            if task.done():
                task.result()
            if time.monotonic() > deadline:
                raise AssertionError("uvicorn did not start")
            await asyncio.sleep(0.02)
        base = f"http://127.0.0.1:{port}"
        client = httpx.AsyncClient(
            base_url=base, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30
        )
        host = Host(settings=settings, client=client, base_url=base, server=server, task=task)
        started.append(host)
        return host

    yield _start
    for host in started:
        await host.client.aclose()
        if not host.task.done():
            await host.stop()


@pytest.fixture
async def host(start_host: HostFactory) -> Host:
    started: Host = await start_host()
    return started


@pytest.fixture
def sse() -> Callable[[str], list[dict[str, Any]]]:
    """The SSE parser, for tests (a test module cannot import this conftest)."""
    return parse_sse


@pytest.fixture
def settings_for(tmp_path: Path) -> Callable[..., HermarchusSettings]:
    def _make(**overrides: Any) -> HermarchusSettings:
        return make_settings(tmp_path, **overrides)

    return _make
