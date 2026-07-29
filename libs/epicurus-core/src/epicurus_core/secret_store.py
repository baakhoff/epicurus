"""OpenBao secret client — tenant-scoped secret access (the inbound platform API).

OpenBao is a Vault-compatible secrets engine, so this wraps the mature ``hvac``
client rather than hand-rolling HTTP. Secret paths are tenant-scoped via
:func:`scope_secret_path` (``tenants/<tenant>/<base>``), so a module only ever
reaches its own tenant's secrets. ``hvac`` is synchronous; calls run in a worker
thread to stay async-friendly.

Modules fetch their secrets through this — they never read model/API keys from
env or git (see the non-negotiables).

**Token maintenance (#728).** The app token minted by
``infra/compose/scripts/openbao-bootstrap.sh`` is *periodic*: its lifetime is unlimited,
but each lease still expires after the period (768h), so a long-lived process must renew
it or every secret read starts failing with a 403 one month after bootstrap.
:meth:`SecretStore.run_token_renewal` is that loop — core-app starts it from its lifespan,
and because every holder shares one token, that single renewer covers them all. Any 403
additionally triggers one re-auth + retry, so a rotated token (via ``OPENBAO_TOKEN_FILE``)
takes effect without a process bounce.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any, TypeVar

import hvac
from hvac.exceptions import Forbidden, InvalidPath, VaultError

from epicurus_core.config import CoreSettings
from epicurus_core.logging import get_logger
from epicurus_core.tenancy import scope_secret_path

__all__ = ["SecretError", "SecretStore"]

log = get_logger("epicurus_core.secret_store")

_T = TypeVar("_T")

# Retry cadence after a failed renewal. The lease has ~a month of runway, so the retry is
# about catching a transient outage early, not about racing a deadline.
_RENEW_RETRY_INTERVAL_S = 3600.0

# Warn on the first failure of a run, then only every Nth. Renewal has a month of runway,
# so the operator needs a signal that *persists* across days — but one line per attempt
# would drown the log. Bounded, never silent.
_RENEW_WARN_EVERY = 7

# A 403 reads like a policy or path problem and almost never is one: the token is the
# usual suspect. Named in the error so the next operator does not have to rediscover #728.
_TOKEN_HINT = (
    "OpenBao rejected the token (HTTP 403) — the usual cause is an expired or revoked app "
    "token, not a missing secret or a policy gap. The token is periodic and the core renews "
    "it on a schedule; see docs/infrastructure/secrets.md for how to mint a replacement"
)


class SecretError(RuntimeError):
    """Raised when a secret cannot be read or written (missing, auth, or backend error)."""


def _secret_error(action: str, scoped: str, exc: VaultError) -> SecretError:
    """Uniform :class:`SecretError`; a 403 additionally names the token as the likely cause."""
    detail = f"failed to {action} secret {scoped}: {exc}"
    if isinstance(exc, Forbidden):
        detail = f"{detail} — {_TOKEN_HINT}"
    return SecretError(detail)


class SecretStore:
    """Tenant-scoped access to secrets stored in OpenBao (KV v2)."""

    def __init__(
        self,
        url: str = "http://localhost:8200",
        token: str | None = None,
        *,
        mount_point: str = "secret",
        token_provider: Callable[[], str | None] | None = None,
        renew_interval_s: float = 86_400.0,
    ) -> None:
        self._url = url
        self._token = token
        self._token_provider = token_provider
        self._mount_point = mount_point
        self._renew_interval_s = renew_interval_s
        self._client: hvac.Client | None = None
        # hvac calls run in worker threads, so the cached client is shared across them:
        # guard build/invalidate so a burst of 403s re-auths once, not once per thread.
        self._client_lock = threading.Lock()
        self._renew_failures = 0

    @classmethod
    def from_settings(cls, settings: CoreSettings) -> SecretStore:
        """Build from settings; the token may come from ``OPENBAO_TOKEN`` or
        ``OPENBAO_TOKEN_FILE`` (a mounted file, e.g. a Docker secret).

        The token is re-resolved on every client rebuild, so rotating the file behind
        ``OPENBAO_TOKEN_FILE`` takes effect at the next re-auth without a restart. A value
        passed through ``OPENBAO_TOKEN`` is fixed for the life of the process — changing it
        means recreating the container, not restarting it.
        """
        return cls(
            settings.openbao_url,
            token_provider=settings.resolve_openbao_token,
            renew_interval_s=settings.openbao_renew_interval_s,
        )

    # ── client lifecycle ──────────────────────────────────────────────────────────────

    def _resolve_token(self) -> str | None:
        if self._token_provider is None:
            return self._token
        try:
            return self._token_provider()
        except OSError as exc:  # e.g. OPENBAO_TOKEN_FILE vanished under us
            raise SecretError(f"could not read the OpenBao token: {exc}") from exc

    def _client_sync(self) -> hvac.Client:
        """Build the client and verify auth once; reuse it afterwards.

        Rebuilt on demand after a 403 (see :meth:`_invalidate`), re-resolving the token so a
        rotated one takes effect in place.
        """
        with self._client_lock:
            if self._client is None:
                client = hvac.Client(url=self._url, token=self._resolve_token())
                if not client.is_authenticated():
                    raise SecretError(
                        "OpenBao client is not authenticated — the token was rejected. It has "
                        f"most likely expired or been revoked. {_TOKEN_HINT}"
                    )
                self._client = client
            return self._client

    def _invalidate(self, stale: hvac.Client) -> None:
        """Drop *stale* so the next call re-auths — a no-op if another thread already
        rebuilt, otherwise concurrent 403s would each rebuild in turn."""
        with self._client_lock:
            if self._client is stale:
                self._client = None

    def _call(self, op: Callable[[hvac.Client], _T]) -> _T:
        """Run *op* against the cached client; on a 403, re-auth once and retry once.

        A renewed or rotated token takes effect here without a process bounce; a genuinely
        dead token still fails, with the 403 the caller expects.
        """
        client = self._client_sync()
        try:
            return op(client)
        except Forbidden:
            self._invalidate(client)
        # Retried outside the ``except`` so a second failure surfaces alone, rather than
        # chained onto the first as "during handling of the above exception".
        return op(self._client_sync())

    # ── token maintenance (#728) ──────────────────────────────────────────────────────

    async def renew_self(self) -> int:
        """Renew the token's own lease; returns the granted TTL in seconds.

        The bootstrap mints a *periodic* token, so each renewal resets the lease to the full
        period — an unbounded lifetime for as long as something keeps renewing inside it.
        """

        def _renew() -> int:
            def _op(client: hvac.Client) -> int:
                resp = client.auth.token.renew_self()
                auth = resp.get("auth") or {} if isinstance(resp, dict) else {}
                return int(auth.get("lease_duration", 0))

            try:
                return self._call(_op)
            except VaultError as exc:
                detail = f"failed to renew the OpenBao token: {exc}"
                if isinstance(exc, Forbidden):
                    detail = f"{detail} — {_TOKEN_HINT}"
                raise SecretError(detail) from exc

        return await asyncio.to_thread(_renew)

    async def _is_renewable(self) -> bool | None:
        """Whether the token carries a renewable lease; ``None`` if the lookup failed.

        A dev-mode root token is *not* renewable and never expires, so renewing it is a
        permanent error rather than a fault worth retrying — the loop uses this to bow out
        quietly instead of warning forever.
        """

        def _lookup() -> bool | None:
            try:
                resp = self._call(lambda client: client.auth.token.lookup_self())
            except (SecretError, VaultError) as exc:
                log.debug("openbao token lookup failed", error=str(exc))
                return None
            data = resp.get("data") or {} if isinstance(resp, dict) else {}
            renewable = data.get("renewable")
            return bool(renewable) if isinstance(renewable, bool) else None

        return await asyncio.to_thread(_lookup)

    def _note_renew_success(self, ttl: int) -> None:
        """Record a successful renewal. Synchronous by design — see :meth:`run_token_renewal`."""
        if self._renew_failures:
            log.info(
                "openbao token renewal recovered",
                after_failures=self._renew_failures,
                ttl_s=ttl,
            )
        else:
            log.debug("openbao token renewed", ttl_s=ttl)
        self._renew_failures = 0

    def _note_renew_failure(self, exc: BaseException) -> None:
        """Record a failed renewal, warning on the first and every ``_RENEW_WARN_EVERY``th."""
        self._renew_failures += 1
        if self._renew_failures == 1 or self._renew_failures % _RENEW_WARN_EVERY == 0:
            log.warning(
                "openbao token renewal failed; every secret read fails once the lease expires",
                error=str(exc),
                consecutive_failures=self._renew_failures,
            )
        else:
            log.debug(
                "openbao token renewal failed",
                error=str(exc),
                consecutive_failures=self._renew_failures,
            )

    async def run_token_renewal(self) -> None:
        """Keep the token's lease alive on a fixed cadence, forever.

        The bootstrap token is periodic — unlimited *lifetime*, but each lease expires after
        the period (768h by default), so an unrenewed deployment loses every secret read one
        month after bootstrap (#728). Renewing well inside that window leaves weeks of runway,
        so a failure is a warning and never a crash: the loop retries, and repeats are
        throttled so a persistent fault cannot flood the log.

        Cancel the task to stop it. Nothing is awaited on the cancellation path — the bookkeeping
        helpers are deliberately synchronous — so cancellation can never strand the loop.
        """
        if self._renew_interval_s <= 0:
            log.info("openbao token renewal disabled by configuration")
            return
        if await self._is_renewable() is False:
            log.info("openbao token is not renewable (root or non-expiring); renewal not needed")
            return
        while True:
            try:
                ttl = await self.renew_self()
            except Exception as exc:  # a background loop must outlive any failure
                # CancelledError derives from BaseException, so it is never caught here.
                self._note_renew_failure(exc)
                delay = min(self._renew_interval_s, _RENEW_RETRY_INTERVAL_S)
            else:
                self._note_renew_success(ttl)
                delay = self._renew_interval_s
            await asyncio.sleep(delay)

    # ── secrets ───────────────────────────────────────────────────────────────────────

    async def get(self, path: str, tenant_id: str | None = None) -> dict[str, Any]:
        """Read a secret's data. Raises :class:`SecretError` if it does not exist."""
        scoped = scope_secret_path(path, tenant_id)

        def _read() -> dict[str, Any]:
            def _op(client: hvac.Client) -> dict[str, Any]:
                resp = client.secrets.kv.v2.read_secret_version(
                    path=scoped, mount_point=self._mount_point, raise_on_deleted_version=True
                )
                data: dict[str, Any] = resp["data"]["data"]
                return data

            try:
                return self._call(_op)
            except InvalidPath as exc:
                raise SecretError(f"secret not found: {scoped}") from exc
            except VaultError as exc:
                raise _secret_error("read", scoped, exc) from exc

        return await asyncio.to_thread(_read)

    async def set(self, path: str, data: dict[str, Any], tenant_id: str | None = None) -> None:
        """Create or update a secret."""
        scoped = scope_secret_path(path, tenant_id)

        def _write() -> None:
            def _op(client: hvac.Client) -> None:
                client.secrets.kv.v2.create_or_update_secret(
                    path=scoped, secret=data, mount_point=self._mount_point
                )

            try:
                self._call(_op)
            except VaultError as exc:
                raise _secret_error("write", scoped, exc) from exc

        await asyncio.to_thread(_write)

    async def delete(self, path: str, tenant_id: str | None = None) -> None:
        """Delete a secret and all its versions."""
        scoped = scope_secret_path(path, tenant_id)

        def _delete() -> None:
            def _op(client: hvac.Client) -> None:
                client.secrets.kv.v2.delete_metadata_and_all_versions(
                    path=scoped, mount_point=self._mount_point
                )

            try:
                self._call(_op)
            except VaultError as exc:
                raise _secret_error("delete", scoped, exc) from exc

        await asyncio.to_thread(_delete)
