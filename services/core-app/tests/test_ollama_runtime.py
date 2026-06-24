"""Unit tests for OllamaRuntime — write Ollama's KV-cache env file and restart it (#307).

Docker is a duck-typed fake (records restart calls), and the env file lands in ``tmp_path`` —
no socket or container needed. These assert the file content (incl. auto flash attention), the
default/clear path, and graceful degradation when Docker or the filesystem is unavailable.
"""

from __future__ import annotations

from pathlib import Path

from epicurus_core_app.docker_control import DockerError
from epicurus_core_app.llm.ollama_runtime import OllamaRuntime


class _FakeDocker:
    def __init__(self, *, result: bool = True, boom: bool = False) -> None:
        self.restarted: list[str] = []
        self._result = result
        self._boom = boom

    def restart_service(self, name: str) -> bool:
        if self._boom:
            raise DockerError("no socket")
        self.restarted.append(name)
        return self._result


def _runtime(tmp_path: Path, docker: object | None) -> tuple[OllamaRuntime, Path]:
    env = tmp_path / "ollama.env"
    return OllamaRuntime(docker, env_path=str(env), service="ollama"), env  # type: ignore[arg-type]


def test_apply_quantized_writes_env_with_flash_attention(tmp_path: Path) -> None:
    docker = _FakeDocker()
    rt, env = _runtime(tmp_path, docker)
    assert rt.apply_kv_cache_type("q8_0") is True
    assert docker.restarted == ["ollama"]
    content = env.read_text()
    assert "OLLAMA_KV_CACHE_TYPE=q8_0" in content
    assert "OLLAMA_FLASH_ATTENTION=1" in content  # quantized cache requires flash attention


def test_apply_q4_also_enables_flash(tmp_path: Path) -> None:
    rt, env = _runtime(tmp_path, _FakeDocker())
    rt.apply_kv_cache_type("q4_0")
    assert "OLLAMA_FLASH_ATTENTION=1" in env.read_text()


def test_apply_none_removes_the_file(tmp_path: Path) -> None:
    docker = _FakeDocker()
    rt, env = _runtime(tmp_path, docker)
    env.write_text("OLLAMA_KV_CACHE_TYPE=q8_0\n")  # a prior non-default choice
    assert rt.apply_kv_cache_type(None) is True
    assert not env.exists()  # cleared, so Ollama falls back to the compose defaults
    assert docker.restarted == ["ollama"]


def test_apply_without_docker_writes_file_but_reports_not_applied(tmp_path: Path) -> None:
    rt, env = _runtime(tmp_path, None)
    assert rt.apply_kv_cache_type("q8_0") is False  # no socket → operator restarts manually
    assert env.read_text().startswith("OLLAMA_KV_CACHE_TYPE=q8_0")  # but the choice is persisted


def test_apply_swallows_docker_error(tmp_path: Path) -> None:
    rt, _ = _runtime(tmp_path, _FakeDocker(boom=True))
    assert rt.apply_kv_cache_type("q8_0") is False  # restart failed → saved, not applied


def test_apply_swallows_write_error(tmp_path: Path) -> None:
    docker = _FakeDocker()
    blocker = tmp_path / "blocker"
    blocker.write_text("x")  # a file where a directory is expected → mkdir/write raises OSError
    rt = OllamaRuntime(docker, env_path=str(blocker / "nested" / "ollama.env"))  # type: ignore[arg-type]
    assert rt.apply_kv_cache_type("q8_0") is False  # write failed → not applied
    assert docker.restarted == []  # and the restart is never reached
