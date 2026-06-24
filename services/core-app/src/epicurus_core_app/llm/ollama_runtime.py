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
is still persisted — :meth:`apply_kv_cache_type` reports it was not applied, so the UI can fall
back to the manual-restart instructions instead of failing.
"""

from __future__ import annotations

from pathlib import Path

from epicurus_core import get_logger
from epicurus_core_app.docker_control import DockerController, DockerError

log = get_logger("epicurus_core_app.llm.ollama_runtime")

# KV-cache quantization needs flash attention; f16 (or unset) keeps stock behaviour.
_NEEDS_FLASH_ATTENTION = frozenset({"q8_0", "q4_0"})


class OllamaRuntime:
    """Writes Ollama's start-up env file and restarts the container to apply it."""

    def __init__(
        self, docker: DockerController | None, *, env_path: str, service: str = "ollama"
    ) -> None:
        self._docker = docker
        self._env_path = Path(env_path)
        self._service = service

    def apply_kv_cache_type(self, kv_cache_type: str | None) -> bool:
        """Apply ``kv_cache_type`` to the live Ollama runtime; return whether it was applied.

        Writes (or clears) the shared env file, then restarts Ollama so it re-reads it. Returns
        ``False`` — leaving the operator the manual restart path — when Docker is not wired (no
        socket) or the file/volume is not writable, so an incomplete setup degrades gracefully
        instead of failing the request.
        """
        try:
            self._write_env_file(kv_cache_type)
        except OSError as exc:  # volume not mounted / not writable — degrade, don't fail
            log.warning(
                "could not write ollama env file; choice saved but not applied",
                path=str(self._env_path),
                error=str(exc),
            )
            return False
        if self._docker is None:
            return False
        try:
            return self._docker.restart_service(self._service)
        except DockerError as exc:
            log.warning("could not restart ollama; choice saved but not applied", error=str(exc))
            return False

    def _write_env_file(self, kv_cache_type: str | None) -> None:
        """Render the env file Ollama sources at start, or remove it for the default.

        ``None`` (the f16 default) removes the file so Ollama falls back to the compose-level
        defaults; a quantized type also enables flash attention, which it requires.
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
