"""First-boot model bootstrap — make sure the deployment's default local models exist (#773).

Models are never baked into the Ollama image (infra/ollama/compose.yaml): the core owns the
model lifecycle (constraint #8, ADR-0010). But on a *fresh* install the volume starts empty,
so the first embedding or chat call 404s until someone finds the Models page — and background
work (the knowledge indexer, memory recall) fails noisily in the meantime. This task closes
the gap the architecture's way (ADR-0118): at startup the core resolves the models this
deployment actually *defaults to*, pulls the missing ones through the same gateway path the
Models page uses, and applies the same post-pull context suggestion (#386).

Fire-and-forget from the lifespan: it never blocks startup, readiness, or a live turn. A
flaky network gets bounded per-model retries with exponential backoff; exhaustion is a loud
warning, not a crash — the operator can always pull from the Models page instead. Hosted
model ids (``claude/…``) are skipped: pulling only means anything for the local runtime.

``auto`` seeds an *empty* runtime only (#923, ADR-0118 amendment): the moment ``/api/tags``
reports any installed model, ``auto`` no-ops, full stop — it never diffs against the
effective defaults again. Before this fix, "first-boot" was a docstring, not a guard:
``_run`` diffed the effective defaults against the runtime on *every* start, so a model the
operator deliberately deleted on the Models page was silently pulled back on the next
restart — and a restart is routine (an update reconcile, a Compose ``up``, a Kubernetes
rollout, ADR-0134). The runtime's own tag list is the only state consulted (constraint #2 —
no marker on local disk, no extra table); a from-scratch install still gets its defaults
exactly as ADR-0118 intends. An explicit ``LLM_BOOTSTRAP_MODELS`` list keeps today's
ensure-every-boot semantics unchanged — it is a stated pin, not an inferred default, so it
may re-pull an operator's named model on every start.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from epicurus_core import get_logger
from epicurus_core_app.llm import providers as registry
from epicurus_core_app.llm.errors import LocalRuntimeState
from epicurus_core_app.llm.models import ModelInfo

log = get_logger("epicurus_core_app.llm.bootstrap")


class _Gateway(Protocol):
    """The four gateway methods (and one property) the bootstrap needs (narrow, for tests)."""

    @property
    def local_runtime_enabled(self) -> bool: ...

    async def local_runtime_state(self) -> LocalRuntimeState: ...

    async def models(
        self, tenant_id: str | None = None, *, with_capabilities: bool = False
    ) -> list[ModelInfo]: ...

    async def pull(self, model: str) -> None: ...

    async def effective_default(self, tenant_id: str | None = None) -> str: ...

    async def effective_embed_default(self, tenant_id: str | None = None) -> str: ...


def _tagged(model: str) -> str:
    """The fully-tagged form Ollama reports: a bare name installs as ``<name>:latest``."""
    return model if ":" in model else f"{model}:latest"


class ModelBootstrap:
    """Seed the deployment's default local models into an empty runtime, at startup.

    ``models_spec`` is the raw ``LLM_BOOTSTRAP_MODELS`` setting: ``"auto"`` seeds a runtime
    that reports **no** installed models with the effective chat + embedding defaults (the
    operator's stored prefs, else the env defaults — the same resolution every turn uses),
    then no-ops on every later start once anything is installed — it does not tell a never-
    installed model apart from one the operator removed on purpose, so it stops looking the
    moment the runtime is no longer empty. Blank disables the bootstrap entirely. Anything
    else is a comma-separated explicit list, ensured on *every* start regardless of what is
    already installed — an explicit pin is a standing instruction, not a one-time seed.
    """

    def __init__(
        self,
        gateway: _Gateway,
        *,
        models_spec: str = "auto",
        suggest_context: Callable[[str], Awaitable[int | None]] | None = None,
        attempts: int = 4,
        retry_base_s: float = 15.0,
        ready_timeout_s: float = 180.0,
        poll_interval_s: float = 5.0,
    ) -> None:
        self._gateway = gateway
        self._models_spec = models_spec
        self._suggest_context = suggest_context
        self._attempts = max(1, attempts)
        self._retry_base_s = retry_base_s
        self._ready_timeout_s = ready_timeout_s
        self._poll_interval_s = poll_interval_s

    async def run(self) -> None:
        """The lifespan task body — never raises (except cancellation at shutdown)."""
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a bootstrap bug must never take the core down
            log.error("model bootstrap failed unexpectedly", error=str(exc))

    async def _run(self) -> None:
        if not self._gateway.local_runtime_enabled:
            # No local runtime to seed, by the operator's choice (#962, ADR-0144). One line
            # and out: before this, a hosted-only deployment spent 180 s of every process
            # start polling an address that was never going to answer — the poll loop's own
            # docstring said "a hosted-only deployment may run no Ollama at all" while having
            # no way to know that it was one.
            log.info("model bootstrap skipped: this deployment runs no local LLM runtime")
            return

        spec = self._models_spec.strip()
        if not spec:
            log.info("model bootstrap disabled (LLM_BOOTSTRAP_MODELS is blank)")
            return

        installed = await self._wait_for_runtime()
        if installed is None:
            log.warning(
                "local runtime unreachable; skipping model bootstrap",
                waited_s=self._ready_timeout_s,
            )
            return

        if spec.lower() == "auto" and installed:
            # A provisioned runtime, whatever the operator has done to it since: `auto`
            # seeds an empty install, never a running one (#923). The count is logged so
            # the no-op — and the choice behind it — is visible, not silent.
            log.info(
                "model bootstrap: runtime already has models — auto seeds an empty "
                "runtime only, skipping",
                installed_count=len(installed),
            )
            return

        wanted = await self._resolve_wanted(spec)
        missing = [m for m in wanted if _tagged(m) not in installed]
        if not missing:
            log.info("model bootstrap: nothing to do", wanted=wanted)
            return

        log.info("model bootstrap: pulling missing default models", missing=missing)
        for model in missing:
            if await self._pull_with_retries(model):
                await self._apply_context_suggestion(model)

    async def _wait_for_runtime(self) -> set[str] | None:
        """Poll the runtime until it answers, returning the installed (tagged) model names.

        ``None`` after the deadline — a configured runtime that is still down when the clock
        runs out costs one warning, not a crash loop. Only reached when a runtime *is*
        configured: the absent case returns from :meth:`_run` before any of this.

        It polls the **state**, not a raised exception. ``models()`` no longer raises for a
        runtime that cannot answer (#962) — it reports an empty list, which is the truth for
        a caller asking what is installed and a trap for a caller asking whether the runtime
        is up: an Ollama container thirty seconds into its own start-up would have read as
        "an empty runtime", and the bootstrap would have raced it with a pull instead of
        waiting the few seconds it needed.
        """
        deadline = asyncio.get_running_loop().time() + self._ready_timeout_s
        while True:
            if await self._gateway.local_runtime_state() == "ok":
                return {info.name for info in await self._gateway.models()}
            if asyncio.get_running_loop().time() >= deadline:
                log.debug("runtime still unreachable at deadline", waited_s=self._ready_timeout_s)
                return None
            await asyncio.sleep(self._poll_interval_s)

    async def _resolve_wanted(self, spec: str) -> list[str]:
        """The models to ensure: the effective defaults for ``auto``, else the explicit list.

        Hosted-prefixed ids are dropped (they are not pullable into the local runtime);
        duplicates collapse on their tagged form so ``llama3.2`` and ``llama3.2:latest``
        count once.
        """
        if spec.lower() == "auto":
            candidates = [
                await self._gateway.effective_default(),
                await self._gateway.effective_embed_default(),
            ]
        else:
            candidates = [m.strip() for m in spec.split(",")]

        wanted: list[str] = []
        seen: set[str] = set()
        for model in candidates:
            if not model:
                continue
            if registry.is_hosted(model):
                log.info("model bootstrap: skipping hosted model", model=model)
                continue
            if _tagged(model) in seen:
                continue
            seen.add(_tagged(model))
            wanted.append(model)
        return wanted

    async def _pull_with_retries(self, model: str) -> bool:
        """Pull one model, retrying with exponential backoff; ``True`` on success.

        Sized for this network reality: registry fetches fail transiently (DNS, TLS
        timeouts), and Ollama resumes a partial download on the next attempt, so retrying
        the whole pull is cheap. Exhaustion logs a warning and moves on — the next model
        still gets its chance, and the Models page remains the manual fallback.
        """
        for attempt in range(1, self._attempts + 1):
            try:
                await self._gateway.pull(model)
            except Exception as exc:
                if attempt == self._attempts:
                    log.warning(
                        "model bootstrap: giving up on model — pull it from the Models page",
                        model=model,
                        attempts=self._attempts,
                        error=str(exc),
                    )
                    return False
                delay = self._retry_base_s * (2 ** (attempt - 1))
                log.info(
                    "model bootstrap: pull failed; retrying",
                    model=model,
                    attempt=attempt,
                    retry_in_s=delay,
                    error=str(exc),
                )
                await asyncio.sleep(delay)
            else:
                log.info("model bootstrap: pulled", model=model)
                return True
        return False  # pragma: no cover — the loop always returns

    async def _apply_context_suggestion(self, model: str) -> None:
        """Best-effort parity with a Models-page pull: size the context window to the model.

        The web calls ``POST /llm/model-settings/suggest-context`` when its pull finishes
        (#386); a bootstrap pull applies the same heuristic through the injected hook so a
        first-boot model opens correctly sized too. Failure costs the suggestion, never
        the bootstrap.
        """
        if self._suggest_context is None:
            return
        try:
            await self._suggest_context(model)
        except Exception as exc:
            log.warning(
                "model bootstrap: context suggestion failed",
                model=model,
                error=str(exc),
            )
