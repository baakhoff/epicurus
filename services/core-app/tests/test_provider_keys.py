"""Unit tests for provider key management on the gateway (SecretStore faked)."""

from __future__ import annotations

from typing import Any

import pytest

from epicurus_core import SecretError
from epicurus_core_app.llm.gateway import LlmGateway, UnknownProviderError
from epicurus_core_app.llm.power import PowerController


class _FakeSecrets:
    def __init__(self) -> None:
        self.stored: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []

    async def get(self, path: str, tenant_id: str | None = None) -> dict[str, Any]:
        if path not in self.stored:
            raise SecretError("missing")
        return self.stored[path]

    async def set(self, path: str, data: dict[str, Any], tenant_id: str | None = None) -> None:
        self.stored[path] = data

    async def delete(self, path: str, tenant_id: str | None = None) -> None:
        self.deleted.append(path)
        self.stored.pop(path, None)


class _FakeBus:
    async def publish(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        pass


def _gateway(secrets: _FakeSecrets) -> LlmGateway:
    return LlmGateway(
        ollama_url="http://localhost:11434",
        default_model="llama3.2",
        keep_alive="5m",
        power=PowerController(),
        secrets=secrets,  # type: ignore[arg-type]
        default_tenant="local",
        bus=_FakeBus(),  # type: ignore[arg-type]
        fallbacks=[],
    )


async def test_set_key_stores_at_the_provider_path() -> None:
    secrets = _FakeSecrets()
    await _gateway(secrets).set_provider_key("claude", api_key="sk-x")
    assert secrets.stored["llm/anthropic"] == {"api_key": "sk-x"}


async def test_custom_provider_requires_base_url() -> None:
    secrets = _FakeSecrets()
    gateway = _gateway(secrets)
    with pytest.raises(ValueError):
        await gateway.set_provider_key("custom", api_key="sk-x")
    await gateway.set_provider_key("custom", api_key="sk-x", api_base="http://llm.local/v1")
    assert secrets.stored["llm/custom"]["api_base"] == "http://llm.local/v1"


async def test_unknown_and_local_aliases_are_rejected() -> None:
    gateway = _gateway(_FakeSecrets())
    with pytest.raises(UnknownProviderError):
        await gateway.set_provider_key("nope", api_key="k")
    with pytest.raises(UnknownProviderError):
        await gateway.set_provider_key("local", api_key="k")
    with pytest.raises(UnknownProviderError):
        await gateway.clear_provider_key("nope")


async def test_clear_key_deletes_the_secret() -> None:
    secrets = _FakeSecrets()
    gateway = _gateway(secrets)
    await gateway.set_provider_key("gpt", api_key="sk-1")
    await gateway.clear_provider_key("gpt")
    assert "llm/openai" in secrets.deleted
    providers = await gateway.providers()
    assert next(p for p in providers if p.alias == "gpt").configured is False
