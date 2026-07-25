"""Unit tests for OllamaRuntime — write Ollama's KV-cache env file and restart it (#307).

Docker is a duck-typed fake (records restart calls), and the env file lands in ``tmp_path`` —
no socket or container needed. These assert the file content (incl. auto flash attention), the
default/clear path, and graceful degradation when Docker or the filesystem is unavailable.

The degradation is deliberately **two** outcomes, not one (#709): "staged" (the env file holds
the choice, so a container restart applies it) and "not staged" (the file could not be written
at all, the only case that needs manual environment variables).
"""

from __future__ import annotations

from pathlib import Path

from epicurus_core_app.docker_control import DockerError
from epicurus_core_app.llm.ollama_runtime import KvCacheApplyResult, OllamaRuntime


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
    assert rt.apply_kv_cache_type("q8_0") == KvCacheApplyResult(applied=True, staged=True)
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
    assert rt.apply_kv_cache_type(None) == KvCacheApplyResult(applied=True, staged=True)
    assert not env.exists()  # cleared, so Ollama falls back to the compose defaults
    assert docker.restarted == ["ollama"]


def test_apply_without_docker_stages_the_choice(tmp_path: Path) -> None:
    """The usual degraded install: no socket, but the file is written — only a restart is left."""
    rt, env = _runtime(tmp_path, None)
    assert rt.apply_kv_cache_type("q8_0") == KvCacheApplyResult(applied=False, staged=True)
    assert env.read_text().startswith("OLLAMA_KV_CACHE_TYPE=q8_0")


def test_apply_swallows_docker_error_but_stays_staged(tmp_path: Path) -> None:
    rt, env = _runtime(tmp_path, _FakeDocker(boom=True))
    # The restart failed, but the file was written first, so the operator still only restarts.
    assert rt.apply_kv_cache_type("q8_0") == KvCacheApplyResult(applied=False, staged=True)
    assert env.read_text().startswith("OLLAMA_KV_CACHE_TYPE=q8_0")


def test_a_no_op_restart_is_staged_not_applied(tmp_path: Path) -> None:
    """``restart_service`` returns False when no matching container exists — nothing is running
    the new value, but the file is written, so this is the staged case too."""
    rt, _ = _runtime(tmp_path, _FakeDocker(result=False))
    assert rt.apply_kv_cache_type("q8_0") == KvCacheApplyResult(applied=False, staged=True)


def test_clearing_to_the_default_without_docker_is_also_staged(tmp_path: Path) -> None:
    """The unlink path stages exactly like the write path — the same distinction applies (#709)."""
    rt, env = _runtime(tmp_path, None)
    env.write_text("OLLAMA_KV_CACHE_TYPE=q8_0\n")
    assert rt.apply_kv_cache_type(None) == KvCacheApplyResult(applied=False, staged=True)
    assert not env.exists()  # the revert is on disk; only the restart is missing


def test_apply_swallows_write_error_and_is_not_staged(tmp_path: Path) -> None:
    """The one case that really does need manual environment variables."""
    docker = _FakeDocker()
    blocker = tmp_path / "blocker"
    blocker.write_text("x")  # a file where a directory is expected → mkdir/write raises OSError
    rt = OllamaRuntime(docker, env_path=str(blocker / "nested" / "ollama.env"))  # type: ignore[arg-type]
    assert rt.apply_kv_cache_type("q8_0") == KvCacheApplyResult(applied=False, staged=False)
    assert docker.restarted == []  # and the restart is never reached


def test_applied_always_implies_staged(tmp_path: Path) -> None:
    """The invariant the UI branches on: there is no applied-but-unstaged state."""
    for docker in (_FakeDocker(), _FakeDocker(result=False), _FakeDocker(boom=True), None):
        rt, _ = _runtime(tmp_path, docker)
        result = rt.apply_kv_cache_type("q8_0")
        assert not result.applied or result.staged
