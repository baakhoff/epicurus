"""The module-facing reads answer for the tenant the module names (#917).

A module reads four things about itself through ``PlatformClient`` — its stored settings, a
model slot, the review switch and its collection prefs — and has always sent ``tenant_id``
on each. The core dropped it and answered from the registry's one tenant. Since #917 a tool
call runs with the caller's tenant bound and the module's ``PlatformClient`` names it, so
these routes must answer for *that* tenant: unnamed falls back to the registry's tenant
(every web-shell read, every pre-#917 module), malformed is a 400.

The registry's stores are faked in memory, keyed by tenant, so a read that ignored the
tenant would visibly return the other tenant's value.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from epicurus_core import CollectionPrefs, CollectionRef, SecretError
from epicurus_core_app.modules import ModuleRegistry, create_modules_router

DEFAULT = "local"
OTHER = "acme"


class _Secrets:
    """Tenant-keyed in-memory secret store."""

    def __init__(self) -> None:
        self.stored: dict[tuple[str, str | None], dict[str, Any]] = {}

    async def get(self, path: str, tenant_id: str | None = None) -> dict[str, Any]:
        try:
            return self.stored[(path, tenant_id)]
        except KeyError:
            raise SecretError("missing") from None

    async def set(self, path: str, data: dict[str, Any], tenant_id: str | None = None) -> None:
        self.stored[(path, tenant_id)] = data


class _Prefs:
    """Tenant-keyed in-memory stand-in for the three prefs these reads use."""

    def __init__(self) -> None:
        self.models: dict[tuple[str, str], dict[str, str]] = {}
        self.suggestions: dict[tuple[str, str], bool] = {}
        self.collections: dict[tuple[str, str], CollectionPrefs] = {}
        self.asked: list[str] = []

    async def get_models(self, tenant: str, module: str) -> dict[str, str]:
        self.asked.append(tenant)
        return dict(self.models.get((tenant, module), {}))

    async def get_suggestions_enabled(self, tenant: str, module: str) -> bool:
        self.asked.append(tenant)
        return self.suggestions.get((tenant, module), True)

    async def get_collections(self, tenant: str, module: str) -> CollectionPrefs:
        self.asked.append(tenant)
        return self.collections.get((tenant, module), CollectionPrefs())


class _Registry(ModuleRegistry):
    async def _probe(self, base: str) -> Any:  # never reached by these reads
        raise AssertionError("the module-facing reads must not probe the module")


@pytest.fixture
def stores() -> tuple[_Secrets, _Prefs]:
    secrets, prefs = _Secrets(), _Prefs()
    secrets.stored[("modules/notes/config", DEFAULT)] = {"owner": DEFAULT}
    secrets.stored[("modules/notes/config", OTHER)] = {"owner": OTHER}
    prefs.models[(DEFAULT, "notes")] = {"embedding": "default-embed"}
    prefs.models[(OTHER, "notes")] = {"embedding": "acme-embed"}
    prefs.suggestions[(OTHER, "notes")] = False
    prefs.collections[(OTHER, "notes")] = CollectionPrefs(
        enabled=[CollectionRef(account="acme@x", collection="c1")],
        active=CollectionRef(account="acme@x", collection="c1"),
    )
    return secrets, prefs


@pytest.fixture
def client(stores: tuple[_Secrets, _Prefs]) -> TestClient:
    secrets, prefs = stores
    registry = _Registry(
        ["http://notes:8080"],
        mcp=MagicMock(),
        secrets=secrets,  # type: ignore[arg-type]
        tenant=DEFAULT,
        prefs=prefs,  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.include_router(create_modules_router(registry))
    return TestClient(app)


BASE = "/platform/v1/modules/notes"


def test_config_answers_for_the_named_tenant(client: TestClient) -> None:
    assert client.get(f"{BASE}/config", params={"tenant_id": OTHER}).json() == {"owner": OTHER}
    assert client.get(f"{BASE}/config", params={"tenant_id": DEFAULT}).json() == {"owner": DEFAULT}


def test_config_of_a_tenant_with_nothing_saved_is_empty_not_the_defaults(
    client: TestClient,
) -> None:
    assert client.get(f"{BASE}/config", params={"tenant_id": "globex"}).json() == {}


def test_model_slot_answers_for_the_named_tenant(client: TestClient) -> None:
    got = client.get(f"{BASE}/models/embedding", params={"tenant_id": OTHER}).json()
    assert got == {"model": "acme-embed"}


def test_suggestions_enabled_answers_for_the_named_tenant(client: TestClient) -> None:
    assert client.get(f"{BASE}/suggestions-enabled", params={"tenant_id": OTHER}).json() == {
        "enabled": False
    }
    assert client.get(f"{BASE}/suggestions-enabled").json() == {"enabled": True}


def test_collection_prefs_answer_for_the_named_tenant(client: TestClient) -> None:
    got = client.get(f"{BASE}/collections/prefs", params={"tenant_id": OTHER}).json()
    assert got["active"] == {"account": "acme@x", "collection": "c1"}
    assert client.get(f"{BASE}/collections/prefs").json()["active"] is None


@pytest.mark.parametrize(
    "path",
    ["/config", "/models/embedding", "/suggestions-enabled", "/collections/prefs"],
)
def test_unnamed_falls_back_to_the_registry_tenant(
    client: TestClient, stores: tuple[_Secrets, _Prefs], path: str
) -> None:
    """The web shell and every pre-#917 module: no tenant_id (or an empty one) → as before."""
    _secrets, prefs = stores
    assert client.get(f"{BASE}{path}").status_code == 200
    assert client.get(f"{BASE}{path}", params={"tenant_id": ""}).status_code == 200
    assert set(prefs.asked) <= {DEFAULT}


def test_unnamed_config_is_the_registry_tenants(client: TestClient) -> None:
    assert client.get(f"{BASE}/config").json() == {"owner": DEFAULT}
    assert client.get(f"{BASE}/models/embedding").json() == {"model": "default-embed"}


@pytest.mark.parametrize(
    "path",
    ["/config", "/models/embedding", "/suggestions-enabled", "/collections/prefs"],
)
@pytest.mark.parametrize("bad", ["Acme", "acme_corp", "-x", "a" * 64])
def test_a_malformed_tenant_is_a_400(
    client: TestClient, stores: tuple[_Secrets, _Prefs], path: str, bad: str
) -> None:
    _secrets, prefs = stores
    resp = client.get(f"{BASE}{path}", params={"tenant_id": bad})
    assert resp.status_code == 400
    assert "invalid tenant id" in resp.json()["detail"]
    assert prefs.asked == []
