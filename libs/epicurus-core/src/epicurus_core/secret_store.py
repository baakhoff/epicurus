"""OpenBao secret client — tenant-scoped secret access (the inbound platform API).

OpenBao is a Vault-compatible secrets engine, so this wraps the mature ``hvac``
client rather than hand-rolling HTTP. Secret paths are tenant-scoped via
:func:`scope_secret_path` (``tenants/<tenant>/<base>``), so a module only ever
reaches its own tenant's secrets. ``hvac`` is synchronous; calls run in a worker
thread to stay async-friendly.

Modules fetch their secrets through this — they never read model/API keys from
env or git (see the non-negotiables).
"""

from __future__ import annotations

import asyncio
from typing import Any

import hvac
from hvac.exceptions import InvalidPath, VaultError

from epicurus_core.config import CoreSettings
from epicurus_core.tenancy import scope_secret_path

__all__ = ["SecretError", "SecretStore"]


class SecretError(RuntimeError):
    """Raised when a secret cannot be read or written (missing, auth, or backend error)."""


class SecretStore:
    """Tenant-scoped access to secrets stored in OpenBao (KV v2)."""

    def __init__(
        self,
        url: str = "http://localhost:8200",
        token: str | None = None,
        *,
        mount_point: str = "secret",
    ) -> None:
        self._url = url
        self._token = token
        self._mount_point = mount_point
        self._client: hvac.Client | None = None

    @classmethod
    def from_settings(cls, settings: CoreSettings) -> SecretStore:
        return cls(settings.openbao_url, settings.openbao_token)

    def _client_sync(self) -> hvac.Client:
        if self._client is None:
            self._client = hvac.Client(url=self._url, token=self._token)
        if not self._client.is_authenticated():
            raise SecretError("OpenBao client is not authenticated (check the token)")
        return self._client

    async def get(self, path: str, tenant_id: str | None = None) -> dict[str, Any]:
        """Read a secret's data. Raises :class:`SecretError` if it does not exist."""
        scoped = scope_secret_path(path, tenant_id)

        def _read() -> dict[str, Any]:
            try:
                resp = self._client_sync().secrets.kv.v2.read_secret_version(
                    path=scoped, mount_point=self._mount_point, raise_on_deleted_version=True
                )
            except InvalidPath as exc:
                raise SecretError(f"secret not found: {scoped}") from exc
            except VaultError as exc:
                raise SecretError(f"failed to read secret {scoped}: {exc}") from exc
            data: dict[str, Any] = resp["data"]["data"]
            return data

        return await asyncio.to_thread(_read)

    async def set(self, path: str, data: dict[str, Any], tenant_id: str | None = None) -> None:
        """Create or update a secret."""
        scoped = scope_secret_path(path, tenant_id)

        def _write() -> None:
            try:
                self._client_sync().secrets.kv.v2.create_or_update_secret(
                    path=scoped, secret=data, mount_point=self._mount_point
                )
            except VaultError as exc:
                raise SecretError(f"failed to write secret {scoped}: {exc}") from exc

        await asyncio.to_thread(_write)

    async def delete(self, path: str, tenant_id: str | None = None) -> None:
        """Delete a secret and all its versions."""
        scoped = scope_secret_path(path, tenant_id)

        def _delete() -> None:
            try:
                self._client_sync().secrets.kv.v2.delete_metadata_and_all_versions(
                    path=scoped, mount_point=self._mount_point
                )
            except VaultError as exc:
                raise SecretError(f"failed to delete secret {scoped}: {exc}") from exc

        await asyncio.to_thread(_delete)
