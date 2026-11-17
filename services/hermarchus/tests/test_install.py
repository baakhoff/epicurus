"""Install-on-request: runs a kind's install command into the home volume at start, with
the sandboxed environment; failures are reported, never fatal."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

KINDS = """
kinds:
  fake:
    command: fake-agent
    install: >-
      mkdir -p "$HOME/.local/bin" &&
      printf '#!/bin/sh\\nexit 0\\n' > "$HOME/.local/bin/fake-agent" &&
      chmod +x "$HOME/.local/bin/fake-agent" &&
      echo "token=${HERMARCHUS_TOKEN:-absent}"
  broken:
    command: broken-agent
    install: echo boom; exit 7
  noinstall:
    command: nothing-here
"""


async def _info(host: Any) -> dict[str, Any]:
    info: dict[str, Any] = (await host.client.get("/v1/info")).json()
    return {a["kind"]: a for a in info["agents"]}


async def test_install_on_request(start_host: Any, tmp_path: Path, monkeypatch: Any) -> None:
    kinds_file = tmp_path / "kinds.yaml"
    kinds_file.write_text(KINDS)
    monkeypatch.setenv("HERMARCHUS_TOKEN", "the-real-token-0123456789")
    host = await start_host(kinds_file=kinds_file, install_kinds="fake,broken,noinstall,nope,echo")
    deadline = time.monotonic() + 30
    agents = await _info(host)
    while time.monotonic() < deadline:
        agents = await _info(host)
        if all(
            agents[k]["install"]["state"] not in ("pending", "running") for k in ("fake", "broken")
        ):
            break
        await asyncio.sleep(0.1)
    fake, broken = agents["fake"], agents["broken"]
    assert fake["install"]["state"] == "succeeded", fake
    assert fake["available"] is True
    assert "token=absent" in fake["install"]["output_tail"]
    assert broken["install"]["state"] == "failed"
    assert "exited 7" in broken["install"]["detail"]
    assert "boom" in broken["install"]["output_tail"]
    assert agents["noinstall"]["install"]["state"] == "failed"
    assert "no `install`" in agents["noinstall"]["install"]["detail"]
    assert agents["echo"]["install"]["state"] == "failed"
    assert (host.settings.agent_home / ".local" / "bin" / "fake-agent").is_file()
    # The host kept serving throughout.
    sid = await host.create()
    await host.send(sid, "ok")
    await host.wait_message(sid, "echo: ok")

    # A second start skips what is already installed.
    await host.stop()
    again = await start_host(kinds_file=kinds_file, install_kinds="fake")
    assert (await _info(again))["fake"]["install"]["state"] == "skipped"
