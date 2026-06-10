"""Integration tests for the OpenBao SecretStore. Require Docker (testcontainers)."""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx
import hvac
import pytest
from testcontainers.core.container import DockerContainer

from epicurus_core.secret_store import SecretError, SecretStore

pytestmark = pytest.mark.integration

_TOKEN = "test-root"


@pytest.fixture(scope="module")
def openbao_url() -> Iterator[str]:
    container = (
        DockerContainer("openbao/openbao:2.2.0")
        .with_env("BAO_DEV_ROOT_TOKEN_ID", _TOKEN)
        .with_env("BAO_DEV_LISTEN_ADDRESS", "0.0.0.0:8200")
        .with_command(["server", "-dev"])
        .with_exposed_ports(8200)
    )
    with container:
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8200)}"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{url}/v1/sys/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        yield url


async def test_set_get_delete(openbao_url: str) -> None:
    store = SecretStore(openbao_url, _TOKEN)
    secret = {"client_id": "abc", "client_secret": "xyz"}

    await store.set("google/oauth", secret, tenant_id="acme")
    assert await store.get("google/oauth", tenant_id="acme") == secret

    await store.delete("google/oauth", tenant_id="acme")
    with pytest.raises(SecretError):
        await store.get("google/oauth", tenant_id="acme")


async def test_tenant_isolation(openbao_url: str) -> None:
    store = SecretStore(openbao_url, _TOKEN)
    await store.set("api/key", {"value": "acme-only"}, tenant_id="acme")

    # A different tenant's scoped path does not exist.
    with pytest.raises(SecretError):
        await store.get("api/key", tenant_id="other")


async def test_bad_token_raises(openbao_url: str) -> None:
    store = SecretStore(openbao_url, "wrong-token")
    with pytest.raises(SecretError):
        await store.get("api/key", tenant_id="acme")


async def test_authentication_is_checked_once(
    openbao_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    original = hvac.Client.is_authenticated

    def counting(self: hvac.Client) -> bool:
        nonlocal calls
        calls += 1
        return bool(original(self))

    monkeypatch.setattr(hvac.Client, "is_authenticated", counting)
    store = SecretStore(openbao_url, _TOKEN)
    await store.set("auth/check", {"v": "1"}, tenant_id="acme")
    await store.get("auth/check", tenant_id="acme")
    await store.get("auth/check", tenant_id="acme")
    # The auth round-trip happens once, when the client is first built.
    assert calls == 1
