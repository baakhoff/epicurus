"""A failing session records the agent's real exit code, even when stdout closed first.

An agent's stdout can reach EOF (the adapter raises ``AgentExited``) before asyncio has reaped
the process, so ``returncode`` is still ``None`` at that instant. ``LiveSession._fail`` waits,
boundedly, for the exit watch before it records ``session.failed``. Seen in a full-suite run as
``exit_code: None`` in ``test_an_agent_that_crashes_marks_the_session_failed``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from epicurus_hermarchus import runtime
from epicurus_hermarchus.runtime import LiveSession


class _Adapter:
    """Exits with code 3, but reports it only once the exit watch has completed."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        await self._exited.wait()
        await asyncio.sleep(0.05)  # reaped a little after stdout closed
        self.returncode = 3
        return 3

    def stderr_tail(self) -> list[str]:
        return []

    async def close(self, grace: float) -> int | None:
        return self.returncode


class _Manager:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any] | None]] = []
        self.settings = SimpleNamespace(stop_grace_s=1.0)
        self.store = SimpleNamespace(drop_pending_inputs=self._none, update_session=self._update)

    async def _none(self, *_: Any) -> list[str]:
        return []

    async def _update(self, *_: Any, **__: Any) -> None:
        return None

    async def record(self, sid: str, type_: str, data: dict[str, Any] | None) -> None:
        self.events.append((type_, data))

    def forget(self, sid: str) -> None:
        return None


def _session(manager: _Manager, adapter: _Adapter) -> LiveSession:
    s = LiveSession(
        manager,  # type: ignore[arg-type]
        session_id="s1",
        kind=SimpleNamespace(name="stub"),  # type: ignore[arg-type]
        workdir=Path("/nonexistent"),
        cwd_rel=".",
        unattended=False,
    )
    s.adapter = adapter  # type: ignore[assignment]

    async def _no_permissions(_by: str) -> None:
        return None

    s._cancel_permissions = _no_permissions  # type: ignore[method-assign,assignment]
    return s


async def test_session_failed_carries_the_exit_code_reaped_after_stdout_closed() -> None:
    manager, adapter = _Manager(), _Adapter()
    session = _session(manager, adapter)
    session._exit_watch = asyncio.create_task(adapter.wait())
    adapter._exited.set()  # stdout is closed; the process is not reaped yet
    assert adapter.returncode is None
    await session._fail("the agent process exited")
    failed = [d for t, d in manager.events if t == "session.failed"]
    assert failed and failed[0] is not None
    assert failed[0]["exit_code"] == 3


async def test_the_wait_for_the_exit_code_is_bounded(monkeypatch: Any) -> None:
    monkeypatch.setattr(runtime, "EXIT_CODE_WAIT_S", 0.05)
    manager, adapter = _Manager(), _Adapter()
    session = _session(manager, adapter)
    session._exit_watch = asyncio.create_task(adapter.wait())  # never exits
    await asyncio.wait_for(session._fail("gone"), timeout=2)
    failed = [d for t, d in manager.events if t == "session.failed"]
    assert failed and failed[0] is not None and failed[0]["exit_code"] is None
    session._exit_watch.cancel()
