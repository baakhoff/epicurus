"""Apply the operator's Ollama runtime choices (KV-cache type) to the live container.

The Ollama server reads ``OLLAMA_KV_CACHE_TYPE`` / ``OLLAMA_FLASH_ATTENTION`` from its
environment **at startup only** — they are not per-request knobs. #296 stored the operator's
choice but left *applying* it to them (edit ``.env``, restart Ollama). This closes that loop
(#307): the core writes the chosen values to a small env file the Ollama entrypoint sources on
every (re)start — mounted from a named volume both containers share — then restarts the Ollama
container through the tightly-scoped :class:`~epicurus_core_app.docker_control.DockerController`.

A plain ``docker restart`` would *not* re-read env (it is fixed at container create); the Ollama
entrypoint wrapper re-sources the file on each start, so the restart applies the new value and it
persists across reconciles (the file lives in the volume). When Docker is unavailable the choice
is still persisted, and :meth:`apply_kv_cache_type` reports *how far it got* so the UI can give
the operator the right instruction rather than the worst-case one (#709).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from epicurus_core import get_logger
from epicurus_core_app.docker_control import DockerController, DockerError

log = get_logger("epicurus_core_app.llm.ollama_runtime")

# KV-cache quantization needs flash attention; f16 (or unset) keeps stock behaviour.
_NEEDS_FLASH_ATTENTION = frozenset({"q8_0", "q4_0"})


@dataclass(frozen=True, slots=True)
class KvCacheApplyResult:
    """How far applying a KV-cache choice got — there are **two** degraded modes, not one (#709).

    The distinction decides which instruction the operator gets, and getting it wrong is worse
    than saying nothing: the common degraded case (no Docker socket) needs only a container
    restart, because the entrypoint re-sources the env file on every start — telling that
    operator to hand-edit environment variables sends them somewhere they never needed to go.

    * ``staged`` — the env file now reflects the choice (written, or removed for the default).
      A restart of the Ollama container is the only thing left.
    * ``applied`` — the restart also happened, so the running server is using it. Implies
      ``staged``.
    * neither — the file could not be written (volume missing or unwritable). Only here is the
      manual ``OLLAMA_KV_CACHE_TYPE`` / ``OLLAMA_FLASH_ATTENTION`` route the real one.
    """

    applied: bool
    staged: bool


class OllamaRuntime:
    """Writes Ollama's start-up env file and restarts the container to apply it."""

    def __init__(
        self, docker: DockerController | None, *, env_path: str, service: str = "ollama"
    ) -> None:
        self._docker = docker
        self._env_path = Path(env_path)
        self._service = service

    def apply_kv_cache_type(self, kv_cache_type: str | None) -> KvCacheApplyResult:
        """Apply ``kv_cache_type`` to the live Ollama runtime; report how far it got (#709).

        Writes (or clears) the shared env file, then restarts Ollama so it re-reads it. Degrades
        instead of failing the request, in two distinct ways the caller must be able to tell
        apart — see :class:`KvCacheApplyResult`. Never raises.
        """
        try:
            self._write_env_file(kv_cache_type)
        except OSError as exc:  # volume not mounted / not writable — degrade, don't fail
            log.warning(
                "could not write ollama env file; choice saved but not staged",
                path=str(self._env_path),
                error=str(exc),
            )
            return KvCacheApplyResult(applied=False, staged=False)
        # From here the choice is on disk, so a restart of the Ollama container applies it —
        # whatever happens next, the operator never needs to touch environment variables.
        if self._docker is None:
            log.info(
                "ollama env file staged; no docker control, so a container restart applies it",
                path=str(self._env_path),
            )
            return KvCacheApplyResult(applied=False, staged=True)
        try:
            restarted = self._docker.restart_service(self._service)
        except DockerError as exc:
            log.warning("could not restart ollama; choice staged but not applied", error=str(exc))
            return KvCacheApplyResult(applied=False, staged=True)
        return KvCacheApplyResult(applied=restarted, staged=True)

    def _write_env_file(self, kv_cache_type: str | None) -> None:
        """Render the env file Ollama sources at start, or remove it for the default.

        ``None`` (the f16 default) removes the file so Ollama falls back to the compose-level
        defaults; a quantized type also enables flash attention, which it requires. Removal is
        "staging" in exactly the same sense as writing — the on-disk state matches the choice,
        and a restart is what makes it live (#709). Raises ``OSError`` on either path when the
        volume is missing or unwritable; the caller turns that into "not staged".
        """
        if kv_cache_type is None:
            self._env_path.unlink(missing_ok=True)
            return
        flash = "1" if kv_cache_type in _NEEDS_FLASH_ATTENTION else "0"
        self._env_path.parent.mkdir(parents=True, exist_ok=True)
        self._env_path.write_text(
            f"OLLAMA_KV_CACHE_TYPE={kv_cache_type}\nOLLAMA_FLASH_ATTENTION={flash}\n",
            encoding="utf-8",
        )
