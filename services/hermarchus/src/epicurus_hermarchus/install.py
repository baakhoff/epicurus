"""Install-on-request: run named kinds' ``install`` commands into the home volume at start.

``HERMARCHUS_INSTALL_KINDS=claude-code,codex`` runs each kind's configured ``install``
command once per start, in the background, with the same sandboxed environment an agent gets
(HOME = the home volume, ``NPM_CONFIG_PREFIX`` inside it, no service token), so what it
installs lands on the volume and survives a new image. A kind whose command already resolves
is skipped. Nothing here is ever fatal: a failure is logged and reported in ``/v1/info``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import structlog

from epicurus_hermarchus.kinds import AgentKind
from epicurus_hermarchus.sandbox import build_child_env
from epicurus_hermarchus.settings import HermarchusSettings
from epicurus_hermarchus.store import now_iso

__all__ = ["InstallState", "Installer"]

log = structlog.get_logger("hermarchus.install")


@dataclass
class InstallState:
    state: str  # pending | running | succeeded | failed | skipped
    detail: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    output_tail: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "detail": self.detail,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "output_tail": self.output_tail,
        }


class Installer:
    def __init__(self, settings: HermarchusSettings, kinds: dict[str, AgentKind]) -> None:
        self.settings = settings
        self.kinds = kinds
        self.states: dict[str, InstallState] = {}
        self._task: asyncio.Task[None] | None = None
        self._proc: asyncio.subprocess.Process | None = None

    def _env(self) -> dict[str, str]:
        return build_child_env(
            agent_home=self.settings.agent_home,
            parent_env=os.environ,
            passthrough=self.settings.passthrough_names,
            secrets=[self.settings.bearer_token()],
        )

    def start(self) -> None:
        queue: list[AgentKind] = []
        path = self._env()["PATH"]
        for name in self.settings.install_kind_names:
            kind = self.kinds.get(name)
            if kind is None:
                self.states[name] = InstallState("failed", detail=f"unknown agent kind {name!r}")
            elif not kind.install:
                self.states[name] = InstallState(
                    "failed", detail=f"kind {name!r} has no `install` command"
                )
            elif kind.resolve(path) is not None:
                self.states[name] = InstallState(
                    "skipped", detail=f"{kind.command!r} is already installed"
                )
            else:
                self.states[name] = InstallState("pending")
                queue.append(kind)
        for name, st in self.states.items():
            if st.state == "failed":
                log.warning("install.refused", kind=name, detail=st.detail)
        if queue:
            self._task = asyncio.create_task(self._run(queue), name="hermarchus-install")

    async def _run(self, queue: list[AgentKind]) -> None:
        for kind in queue:
            await self._install(kind)

    async def _install(self, kind: AgentKind) -> None:
        assert kind.install is not None
        st = self.states[kind.kind]
        st.state, st.started_at = "running", now_iso()
        log.info("install.started", kind=kind.kind, command=kind.install)
        tail: deque[str] = deque(maxlen=20)
        try:
            self.settings.agent_home.mkdir(parents=True, exist_ok=True)
            self._proc = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-c",
                kind.install,
                cwd=str(self.settings.agent_home),
                env=self._env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            assert self._proc.stdout is not None

            async def _drain(stream: asyncio.StreamReader) -> None:
                async for raw in stream:
                    tail.append(raw.decode("utf-8", "replace").rstrip())

            await asyncio.wait_for(
                asyncio.gather(_drain(self._proc.stdout), self._proc.wait()),
                timeout=self.settings.install_timeout_s,
            )
            code = self._proc.returncode
            if code == 0:
                st.state, st.detail = "succeeded", None
            else:
                st.state, st.detail = "failed", f"the install command exited {code}"
        except TimeoutError:
            if self._proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    self._proc.kill()
            st.state = "failed"
            st.detail = f"the install command ran longer than {self.settings.install_timeout_s:g}s"
        except OSError as exc:
            st.state, st.detail = "failed", f"could not run the install command: {exc}"
        finally:
            self._proc = None
        st.finished_at = now_iso()
        st.output_tail = list(tail)
        if st.state == "succeeded":
            log.info("install.succeeded", kind=kind.kind)
        else:
            log.warning("install.failed", kind=kind.kind, detail=st.detail, output=st.output_tail)

    async def close(self) -> None:
        if self._proc is not None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
