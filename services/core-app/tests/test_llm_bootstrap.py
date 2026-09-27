"""Tests for the first-boot model bootstrap (#773, ADR-0118).

The bootstrap's whole contract is defensive: pull exactly the missing default local
models, tolerate a flaky network and an absent runtime, and never let any failure
escape the lifespan task. Everything here drives it through a fake gateway — the
narrow ``_Gateway`` protocol is the seam.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

from epicurus_core_app.llm.bootstrap import ModelBootstrap
from epicurus_core_app.llm.errors import LocalRuntimeState
from epicurus_core_app.llm.models import ModelInfo


class FakeGateway:
    """A gateway double: canned inventory/defaults, scripted pull failures."""

    def __init__(
        self,
        *,
        installed: list[str] | None = None,
        default: str = "llama3.2",
        embed_default: str = "nomic-embed-text",
        pull_failures: dict[str, int] | None = None,
        reachable: bool = True,
        local_runtime_enabled: bool = True,
    ) -> None:
        self.installed = list(installed or [])
        self.default = default
        self.embed_default = embed_default
        # Model → number of times pull raises before succeeding.
        self.pull_failures = dict(pull_failures or {})
        self.reachable = reachable
        self._local_runtime_enabled = local_runtime_enabled
        self.pull_calls: list[str] = []
        self.models_calls = 0
        self.state_calls = 0

    @property
    def local_runtime_enabled(self) -> bool:
        return self._local_runtime_enabled

    async def local_runtime_state(self) -> LocalRuntimeState:
        self.state_calls += 1
        if not self._local_runtime_enabled:
            return "absent"
        return "ok" if self.reachable else "unreachable"

    async def models(
        self, tenant_id: str | None = None, *, with_capabilities: bool = False
    ) -> list[ModelInfo]:
        self.models_calls += 1
        if not self.reachable:
            raise ConnectionError("runtime down")
        return [ModelInfo(name=name) for name in self.installed]

    async def pull(self, model: str) -> None:
        self.pull_calls.append(model)
        remaining = self.pull_failures.get(model, 0)
        if remaining > 0:
            self.pull_failures[model] = remaining - 1
            raise ConnectionError("registry timeout")
        self.installed.append(model if ":" in model else f"{model}:latest")

    async def effective_default(self, tenant_id: str | None = None) -> str:
        return self.default

    async def effective_embed_default(self, tenant_id: str | None = None) -> str:
        return self.embed_default


def make_bootstrap(
    gateway: FakeGateway,
    *,
    models_spec: str,
    suggest_context: Callable[[str], Awaitable[int | None]] | None = None,
    attempts: int = 4,
    ready_timeout_s: float = 0.05,
) -> ModelBootstrap:
    """A bootstrap with test-speed timings (zero backoff, fast readiness poll)."""
    return ModelBootstrap(
        gateway,
        models_spec=models_spec,
        suggest_context=suggest_context,
        attempts=attempts,
        retry_base_s=0.0,
        ready_timeout_s=ready_timeout_s,
        poll_interval_s=0.01,
    )


async def test_blank_spec_disables_everything() -> None:
    gateway = FakeGateway()
    await make_bootstrap(gateway, models_spec="  ").run()
    assert gateway.models_calls == 0
    assert gateway.pull_calls == []


async def test_auto_pulls_both_defaults_on_an_empty_runtime() -> None:
    # A from-scratch install: nothing installed, both effective defaults get pulled.
    gateway = FakeGateway(installed=[])
    await make_bootstrap(gateway, models_spec="auto").run()
    assert gateway.pull_calls == ["llama3.2", "nomic-embed-text"]


async def test_auto_noops_when_anything_is_already_installed() -> None:
    # #923: a runtime that has been provisioned — even with just one model, even one that
    # is neither effective default — is not "empty" any more; `auto` must not reconsider
    # the defaults at all, so a model the operator deleted on purpose stays deleted.
    gateway = FakeGateway(installed=["some-other-model:latest"])
    await make_bootstrap(gateway, models_spec="auto").run()
    assert gateway.pull_calls == []


async def test_auto_noops_when_everything_is_present() -> None:
    gateway = FakeGateway(installed=["llama3.2:latest", "nomic-embed-text:latest"])
    await make_bootstrap(gateway, models_spec="auto").run()
    assert gateway.pull_calls == []


async def test_auto_does_not_resolve_defaults_when_runtime_is_non_empty() -> None:
    # The no-op happens before `effective_default`/`effective_embed_default` are even
    # consulted — a provisioned runtime skips the resolution entirely, not just the pull.
    class TattlingGateway(FakeGateway):
        async def effective_default(self, tenant_id: str | None = None) -> str:
            raise AssertionError("auto must not resolve defaults on a non-empty runtime")

    gateway = TattlingGateway(installed=["llama3.2:latest"])
    await make_bootstrap(gateway, models_spec="auto").run()
    assert gateway.pull_calls == []


async def test_hosted_defaults_are_skipped() -> None:
    # An operator who defaults chat to a hosted model must not trigger a local pull for it.
    gateway = FakeGateway(default="claude/claude-3-5-sonnet-latest")
    await make_bootstrap(gateway, models_spec="auto").run()
    assert gateway.pull_calls == ["nomic-embed-text"]


async def test_explicit_list_is_parsed_and_deduped() -> None:
    # "qwen3:8b" twice (bare + tagged) collapses to one pull; blanks are ignored.
    gateway = FakeGateway(installed=[])
    await make_bootstrap(gateway, models_spec=" qwen3:8b, ,qwen3:8b ,mistral ").run()
    assert gateway.pull_calls == ["qwen3:8b", "mistral"]


async def test_explicit_list_still_ensures_on_a_non_empty_runtime() -> None:
    # Unlike `auto`, a named pin is a standing instruction: it is checked (and re-pulled if
    # missing) on every start regardless of what else is already installed.
    gateway = FakeGateway(installed=["some-other-model:latest", "mistral:latest"])
    await make_bootstrap(gateway, models_spec="qwen3:8b,mistral").run()
    assert gateway.pull_calls == ["qwen3:8b"]


async def test_unreachable_runtime_gives_up_quietly() -> None:
    gateway = FakeGateway(reachable=False)
    await make_bootstrap(gateway, models_spec="auto").run()
    # It waits on the runtime's *state*, not on `models()` raising: since #962 an
    # unreachable runtime reports an empty list rather than an error, and polling that
    # would read a still-starting Ollama as "an empty runtime" and race it with a pull.
    assert gateway.state_calls >= 1
    assert gateway.models_calls == 0
    assert gateway.pull_calls == []


async def test_pull_retries_until_success() -> None:
    gateway = FakeGateway(pull_failures={"llama3.2": 2})
    await make_bootstrap(gateway, models_spec="auto", attempts=4).run()
    # Two failures, then success — plus the embed default's clean first pull.
    assert gateway.pull_calls.count("llama3.2") == 3
    assert "llama3.2:latest" in gateway.installed


async def test_exhausted_retries_move_on_to_the_next_model() -> None:
    # Chat never succeeds; the embed default must still be pulled and the task must return.
    gateway = FakeGateway(pull_failures={"llama3.2": 99})
    await make_bootstrap(gateway, models_spec="auto", attempts=2).run()
    assert gateway.pull_calls.count("llama3.2") == 2
    assert gateway.pull_calls.count("nomic-embed-text") == 1
    assert "nomic-embed-text:latest" in gateway.installed


async def test_context_suggestion_runs_per_pulled_model() -> None:
    suggested: list[str] = []

    async def suggest(model: str) -> int | None:
        suggested.append(model)
        return 8192

    # An explicit list, not `auto` — `auto` would no-op outright on a non-empty runtime,
    # which would defeat the point of this test (already-present vs. freshly-pulled).
    gateway = FakeGateway(installed=["nomic-embed-text:latest"])
    await make_bootstrap(
        gateway, models_spec="llama3.2,nomic-embed-text", suggest_context=suggest
    ).run()
    # Only the freshly-pulled model is sized; the already-present one is untouched (#386
    # parity: the web only suggests after a pull finishes).
    assert suggested == ["llama3.2"]


async def test_context_suggestion_failure_is_not_fatal() -> None:
    async def suggest(model: str) -> int | None:
        raise RuntimeError("no GPU info")

    gateway = FakeGateway()
    await make_bootstrap(gateway, models_spec="auto", suggest_context=suggest).run()
    assert gateway.pull_calls == ["llama3.2", "nomic-embed-text"]


async def test_run_swallows_unexpected_errors() -> None:
    class ExplodingGateway(FakeGateway):
        async def effective_default(self, tenant_id: str | None = None) -> str:
            raise RuntimeError("prefs store down")

    gateway = ExplodingGateway()
    # Must not raise — the lifespan task wrapper is the last line of defense.
    await make_bootstrap(gateway, models_spec="auto").run()


async def test_cancellation_propagates_for_shutdown() -> None:
    gateway = FakeGateway(reachable=False)
    task = asyncio.create_task(
        make_bootstrap(gateway, models_spec="auto", ready_timeout_s=60.0).run()
    )
    await asyncio.sleep(0.02)  # let it enter the readiness poll
    task.cancel()
    with suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)
    assert task.cancelled()


async def test_no_local_runtime_returns_at_once_without_polling() -> None:
    """A hosted-only deployment spent 180 s of every process start polling nothing (#962).

    Asserted on behaviour, not on a clock: the bootstrap must not probe the runtime's state,
    must not list models, and must not pull — it knows from the configuration alone.
    """
    gateway = FakeGateway(local_runtime_enabled=False)
    await make_bootstrap(gateway, models_spec="auto", ready_timeout_s=30.0).run()
    assert gateway.state_calls == 0
    assert gateway.models_calls == 0
    assert gateway.pull_calls == []


async def test_no_local_runtime_wins_over_an_explicit_model_list() -> None:
    """An explicit pin is a standing instruction — but not one this deployment can carry out."""
    gateway = FakeGateway(local_runtime_enabled=False)
    await make_bootstrap(gateway, models_spec="llama3.2,nomic-embed-text").run()
    assert gateway.pull_calls == []
