"""Token maintenance in the SecretStore: renewal, 403 self-heal, error messages (#728).

Pure unit tests against a scripted stand-in for ``hvac.Client`` — no Docker, unlike the
integration suite in ``test_secret_store.py``. The failure these cover cannot be reproduced
against a real vault in CI: it needs a 32-day-old token, so the defense has to be design.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import hvac
import pytest
from hvac.exceptions import Forbidden, InvalidPath, InvalidRequest

from epicurus_core import secret_store
from epicurus_core.secret_store import SecretError, SecretStore

_LEASE_S = 2_764_800  # 768h, the period the bootstrap mints


class _FakeVault:
    """Scriptable OpenBao stand-in shared by every client the store builds.

    Each operation consumes one entry from its ``*_outcomes`` queue, falling back to its
    default once the queue is empty — so a test scripts only the interesting prefix
    (``[Forbidden(...)]``, meaning "fails once, then works"). An outcome that is an exception
    *class* is raised fresh every time; an instance is raised as given.
    """

    def __init__(self) -> None:
        self.clients: list[_FakeClient] = []
        self.calls: list[str] = []
        self.authenticated = True
        self.outcomes: dict[str, list[Any]] = {}
        self.defaults: dict[str, Any] = {
            "read": {"data": {"data": {"api_key": "secret"}}},
            "write": None,
            "delete": None,
            "renew_self": {"auth": {"lease_duration": _LEASE_S}},
            "lookup_self": {"data": {"renewable": True}},
        }

    @property
    def tokens(self) -> list[str | None]:
        """The token each client generation was built with — rotation is visible here."""
        return [client.token for client in self.clients]

    def count(self, name: str) -> int:
        return self.calls.count(name)

    def script(self, name: str, *outcomes: Any) -> None:
        self.outcomes[name] = list(outcomes)

    def always(self, name: str, outcome: Any) -> None:
        self.defaults[name] = outcome

    def call(self, name: str, **_kwargs: Any) -> Any:
        # Runs in an ``asyncio.to_thread`` worker: append and read counts only. Signalling the
        # test with an ``asyncio.Event`` from here would touch the loop from a foreign thread,
        # and the waiter could wake a step ahead of the coroutine — tests poll instead.
        self.calls.append(name)
        queue = self.outcomes.get(name) or []
        outcome = queue.pop(0) if queue else self.defaults[name]
        if isinstance(outcome, type) and issubclass(outcome, BaseException):
            raise outcome("scripted failure")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeClient:
    """Just enough of ``hvac.Client`` for the paths SecretStore touches."""

    def __init__(self, vault: _FakeVault, url: str, token: str | None) -> None:
        self._vault = vault
        self.url = url
        self.token = token
        kv_v2 = SimpleNamespace(
            read_secret_version=lambda **kw: vault.call("read", **kw),
            create_or_update_secret=lambda **kw: vault.call("write", **kw),
            delete_metadata_and_all_versions=lambda **kw: vault.call("delete", **kw),
        )
        self.secrets = SimpleNamespace(kv=SimpleNamespace(v2=kv_v2))
        self.auth = SimpleNamespace(
            token=SimpleNamespace(
                renew_self=lambda: vault.call("renew_self"),
                lookup_self=lambda: vault.call("lookup_self"),
            )
        )
        vault.clients.append(self)

    def is_authenticated(self) -> bool:
        self._vault.calls.append("is_authenticated")
        return self._vault.authenticated


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> _FakeVault:
    """Install the stand-in in place of ``hvac.Client`` for the duration of a test."""
    fake = _FakeVault()
    monkeypatch.setattr(hvac, "Client", lambda url, token: _FakeClient(fake, url, token))
    return fake


class _LogRecorder:
    """Stand-in for the module logger.

    Monkeypatched over ``secret_store.log`` rather than using structlog's ``capture_logs()``:
    ``cache_logger_on_first_use`` freezes the bound logger the first time it is used, so
    capture_logs sees nothing once another test in the run has already logged.
    """

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []
        self.infos: list[tuple[str, dict[str, Any]]] = []
        self.debugs: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kwargs: Any) -> None:
        self.warnings.append((event, kwargs))

    def info(self, event: str, **kwargs: Any) -> None:
        self.infos.append((event, kwargs))

    def debug(self, event: str, **kwargs: Any) -> None:
        self.debugs.append((event, kwargs))


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    recorder = _LogRecorder()
    monkeypatch.setattr(secret_store, "log", recorder)
    return recorder


def _store(vault: _FakeVault, **kwargs: Any) -> SecretStore:
    return SecretStore("http://openbao:8200", "app-token", **kwargs)


async def _drain(task: asyncio.Task[None]) -> None:
    """Cancel a renewal loop and confirm the cancellation actually took."""
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)


async def _until(what: str, predicate: Callable[[], bool], *, timeout: float = 15.0) -> None:
    """Poll until *predicate* holds, or fail loudly. Never wait on a loop that has stalled."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.01)


# ── 403 self-heal ─────────────────────────────────────────────────────────────────────


async def test_forbidden_read_rebuilds_the_client_and_retries_once(vault: _FakeVault) -> None:
    """The exact production shape: a cached client whose token died mid-flight."""
    vault.script("read", Forbidden("permission denied"))
    store = _store(vault)

    assert await store.get("llm/xai", tenant_id="local") == {"api_key": "secret"}
    assert vault.count("read") == 2, "the read is retried once against a fresh client"
    assert len(vault.clients) == 2, "and the client is rebuilt exactly once"


async def test_forbidden_retry_happens_once_not_in_a_loop(vault: _FakeVault) -> None:
    """A genuinely dead token must fail, not spin: one rebuild, then surface."""
    vault.always("read", Forbidden)
    store = _store(vault)

    with pytest.raises(SecretError) as excinfo:
        await store.get("llm/xai", tenant_id="local")

    assert vault.count("read") == 2
    assert len(vault.clients) == 2
    assert "expired or revoked" in str(excinfo.value), "the 403 names the token, not the path"


async def test_rotated_token_takes_effect_without_a_restart(vault: _FakeVault) -> None:
    """OPENBAO_TOKEN_FILE rotation: the rebuild re-resolves the token, so the new one lands."""
    tokens = iter(["stale-token", "rotated-token"])
    vault.script("read", Forbidden("permission denied"))
    store = SecretStore("http://openbao:8200", token_provider=lambda: next(tokens))

    await store.get("llm/xai", tenant_id="local")

    assert vault.tokens == ["stale-token", "rotated-token"]


async def test_invalidate_is_a_no_op_for_an_already_replaced_client(vault: _FakeVault) -> None:
    """Concurrent 403s must collapse into one rebuild, not one per caller.

    hvac runs in worker threads, so several reads can 403 on the same client at once. Each
    calls ``_invalidate`` with the client *it* used; only the first should clear the cache.
    """
    store = _store(vault)
    first = store._client_sync()
    store._invalidate(first)
    second = store._client_sync()

    store._invalidate(first)  # the loser of the race, arriving late

    assert store._client_sync() is second
    assert len(vault.clients) == 2


async def test_missing_secret_still_reads_as_not_found(vault: _FakeVault) -> None:
    """The 403 hint must not swallow the ordinary "no such secret" case."""
    vault.script("read", InvalidPath("no such path"))
    store = _store(vault)

    with pytest.raises(SecretError, match="secret not found"):
        await store.get("llm/nope", tenant_id="local")
    assert vault.count("read") == 1, "a missing secret is not retried"


async def test_writes_and_deletes_self_heal_too(vault: _FakeVault) -> None:
    """Self-heal belongs to every operation — a rotated token unblocks writes as well."""
    vault.script("write", Forbidden("permission denied"))
    vault.script("delete", Forbidden("permission denied"))
    store = _store(vault)

    await store.set("llm/xai", {"api_key": "k"}, tenant_id="local")
    await store.delete("llm/xai", tenant_id="local")

    assert vault.count("write") == 2
    assert vault.count("delete") == 2


async def test_a_forbidden_write_that_stays_forbidden_names_the_token(vault: _FakeVault) -> None:
    vault.always("write", Forbidden)
    store = _store(vault)

    with pytest.raises(SecretError) as excinfo:
        await store.set("llm/xai", {"api_key": "k"}, tenant_id="local")

    assert "failed to write secret tenants/local/llm/xai" in str(excinfo.value)
    assert "expired or revoked" in str(excinfo.value)


# ── expired-token diagnosis ───────────────────────────────────────────────────────────


async def test_expired_token_at_connect_names_token_lifetime(vault: _FakeVault) -> None:
    """The restart face of #728: the auth preflight fails and core-app refuses to start.

    Before the fix this said only "check the token", which reads like a typo'd value rather
    than a lease that quietly ran out.
    """
    vault.authenticated = False
    store = _store(vault)

    with pytest.raises(SecretError) as excinfo:
        await store.get("llm/xai", tenant_id="local")

    message = str(excinfo.value)
    assert "expired" in message
    assert "periodic" in message, "and points at the renewal model, not just the symptom"


async def test_an_unreadable_token_file_is_a_secret_error(vault: _FakeVault) -> None:
    """A vanished OPENBAO_TOKEN_FILE must not escape as a bare OSError from a worker thread."""

    def _boom() -> str:
        raise FileNotFoundError("/run/secrets/openbao_token")

    store = SecretStore("http://openbao:8200", token_provider=_boom)

    with pytest.raises(SecretError, match="could not read the OpenBao token"):
        await store.get("llm/xai", tenant_id="local")


# ── renewal ───────────────────────────────────────────────────────────────────────────


async def test_renew_self_returns_the_granted_lease(vault: _FakeVault) -> None:
    store = _store(vault)
    assert await store.renew_self() == _LEASE_S


async def test_renew_self_failure_is_a_secret_error(vault: _FakeVault) -> None:
    vault.always("renew_self", InvalidRequest)
    store = _store(vault)

    with pytest.raises(SecretError, match="failed to renew the OpenBao token"):
        await store.renew_self()


@pytest.mark.timeout(30)
async def test_renewal_loop_fires_on_the_configured_cadence(
    vault: _FakeVault, logs: _LogRecorder
) -> None:
    store = _store(vault, renew_interval_s=0.01)

    task = asyncio.create_task(store.run_token_renewal())
    try:
        await _until("three renewals", lambda: vault.count("renew_self") >= 3)
    finally:
        await _drain(task)

    assert not logs.warnings, "a healthy renewal is quiet — debug level only"
    assert len(logs.debugs) >= 3


@pytest.mark.timeout(30)
async def test_repeated_renewal_failures_produce_bounded_warnings(
    vault: _FakeVault, logs: _LogRecorder
) -> None:
    """A month of runway means the operator needs a persistent signal — not one per attempt."""
    target = 15
    vault.always("renew_self", InvalidRequest)
    store = _store(vault, renew_interval_s=0.01)

    task = asyncio.create_task(store.run_token_renewal())
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15
        while store._renew_failures < target and loop.time() < deadline:
            await asyncio.sleep(0.01)
    finally:
        await _drain(task)

    observed = store._renew_failures
    assert observed >= target, "the loop keeps trying rather than giving up"
    assert len(logs.warnings) <= 1 + observed // secret_store._RENEW_WARN_EVERY < observed
    assert "lease expires" in logs.warnings[0][0], "and the first one explains the stakes"


@pytest.mark.timeout(30)
async def test_renewal_recovery_is_reported(vault: _FakeVault, logs: _LogRecorder) -> None:
    vault.script("renew_self", InvalidRequest("openbao is down"))
    store = _store(vault, renew_interval_s=0.01)

    def _recovered() -> bool:
        return any(event == "openbao token renewal recovered" for event, _ in logs.infos)

    task = asyncio.create_task(store.run_token_renewal())
    try:
        await _until("the recovery log line", _recovered)
    finally:
        await _drain(task)

    assert store._renew_failures == 0, "the failure streak resets once renewal works again"
    assert len(logs.warnings) == 1, "the outage warned once, the recovery did not warn at all"


@pytest.mark.timeout(30)
async def test_a_non_renewable_token_stops_the_loop(vault: _FakeVault, logs: _LogRecorder) -> None:
    """A dev-mode root token never expires and cannot be renewed — bow out, don't warn forever."""
    vault.always("lookup_self", {"data": {"renewable": False}})
    store = _store(vault, renew_interval_s=0.01)

    await asyncio.wait_for(store.run_token_renewal(), timeout=15)

    assert vault.count("renew_self") == 0
    assert not logs.warnings
    assert any("not renewable" in event for event, _ in logs.infos)


@pytest.mark.timeout(30)
async def test_a_failed_lookup_does_not_stop_the_loop(vault: _FakeVault) -> None:
    """An unreachable vault at boot must not disable renewal for the life of the process."""
    vault.always("lookup_self", Forbidden)
    store = _store(vault, renew_interval_s=0.01)

    task = asyncio.create_task(store.run_token_renewal())
    try:
        await _until("renewal to start anyway", lambda: vault.count("renew_self") >= 1)
    finally:
        await _drain(task)


async def test_renewal_can_be_disabled(vault: _FakeVault, logs: _LogRecorder) -> None:
    store = _store(vault, renew_interval_s=0)

    await asyncio.wait_for(store.run_token_renewal(), timeout=5)

    assert vault.calls == [], "disabled means no connection at all"
    assert any("disabled" in event for event, _ in logs.infos)


@pytest.mark.timeout(30)
async def test_cancelling_the_loop_is_immediate_and_clean(vault: _FakeVault) -> None:
    """Cancellation must not be swallowed by the loop's ``except Exception`` guard.

    Shutdown cancels the task while it sits in the long inter-renewal sleep — the shape
    core-app's lifespan uses.
    """
    store = _store(vault, renew_interval_s=30)

    task = asyncio.create_task(store.run_token_renewal())
    await _until("the loop to settle into its sleep", lambda: vault.count("renew_self") >= 1)

    await _drain(task)
    assert task.cancelled()
