"""Hosted web search through OpenRouter (#984): the core's search, its endpoint, and the gate.

OpenRouter is faked at the HTTP layer (``httpx.MockTransport``), so these tests pin the exact
request the core sends — the ``openrouter:web_search`` server tool, the key from the tenant's
secret path — and every way the answer is read: citations become results, a missing key is a
structured 409, a provider failure is a 502 that says what the provider said, never an empty
result, and the usage event lands under the caller's tenant.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from epicurus_core import (
    ModuleManifest,
    SecretError,
    SecretNotFoundError,
    UiSection,
    WebSearchResult,
)
from epicurus_core_app.llm.gateway import USAGE_SUBJECT, LlmGateway, UnknownProviderError
from epicurus_core_app.llm.power import PowerController
from epicurus_core_app.llm.web_search import (
    KEY_MISSING,
    KEY_REJECTED,
    KEY_STORE_UNAVAILABLE,
    PROVIDER_ERROR,
    PROVIDER_UNREACHABLE,
    OpenRouterWebSearch,
    WebSearchError,
    parse_citations,
)
from epicurus_core_app.modules import ModuleRegistry, ModuleSnapshot, ModuleStatus
from epicurus_core_app.platform_api import create_platform_router
from epicurus_core_app.settings import CoreAppSettings

# ── fakes ────────────────────────────────────────────────────────────────────────


class _Secrets:
    """Tenant-aware in-memory secret store: ``(tenant, path) -> data``."""

    def __init__(self, stored: dict[tuple[str, str], dict[str, Any]] | None = None) -> None:
        self.stored: dict[tuple[str, str], dict[str, Any]] = dict(stored or {})
        self.reads: list[tuple[str, str | None]] = []
        self.fail_with: Exception | None = None

    async def get(self, path: str, tenant_id: str | None = None) -> dict[str, Any]:
        self.reads.append((path, tenant_id))
        if self.fail_with is not None:
            raise self.fail_with
        key = (tenant_id or "local", path)
        if key not in self.stored:
            raise SecretNotFoundError(f"nothing at {path}")
        return self.stored[key]


class _Bus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any], str | None]] = []

    async def publish(
        self, subject: str, payload: dict[str, Any], *, tenant_id: str | None = None
    ) -> None:
        self.published.append((subject, payload, tenant_id))


def _completion(
    annotations: list[dict[str, Any]] | None = None,
    *,
    searches: int | None = 1,
    model: str = "openai/gpt-4.1-nano",
) -> dict[str, Any]:
    usage: dict[str, Any] = {"prompt_tokens": 120, "completion_tokens": 3}
    if searches is not None:
        usage["server_tool_use"] = {"web_search_requests": searches}
    return {
        "id": "gen-1",
        "model": model,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "done",
                    "annotations": annotations or [],
                }
            }
        ],
        "usage": usage,
    }


def _citation(url: str, title: str = "", content: str = "") -> dict[str, Any]:
    return {
        "type": "url_citation",
        "url_citation": {
            "url": url,
            "title": title,
            "content": content,
            "start_index": 0,
            "end_index": 4,
        },
    }


class _OpenRouter:
    """A fake OpenRouter: records each request and answers with the handler's response."""

    def __init__(self, respond: httpx.Response | Exception) -> None:
        self.respond = respond
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if isinstance(self.respond, Exception):
                raise self.respond
            return self.respond

        return httpx.MockTransport(handler)

    @property
    def body(self) -> dict[str, Any]:
        return dict(json.loads(self.requests[-1].content))


KEY = {"api_key": "sk-or-tenant"}


def _search(
    fake: _OpenRouter,
    *,
    secrets: _Secrets | None = None,
    bus: _Bus | None = None,
    engine: str = "exa",
) -> tuple[OpenRouterWebSearch, _Secrets, _Bus]:
    the_secrets = secrets or _Secrets({("local", "llm/openrouter"): KEY})
    the_bus = bus or _Bus()
    return (
        OpenRouterWebSearch(
            secrets=the_secrets,  # type: ignore[arg-type]
            bus=the_bus,  # type: ignore[arg-type]
            default_tenant="local",
            model="openai/gpt-4.1-nano",
            engine=engine,
            transport=fake.transport(),
        ),
        the_secrets,
        the_bus,
    )


# ── the request ──────────────────────────────────────────────────────────────────


async def test_sends_one_server_tool_search_with_the_tenants_key() -> None:
    fake = _OpenRouter(httpx.Response(200, json=_completion()))
    search, secrets, _ = _search(
        fake, secrets=_Secrets({("acme", "llm/openrouter"): {"api_key": "sk-acme"}})
    )

    await search.search("tidal turbines 2026", max_results=7, tenant_id="acme")

    request = fake.requests[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-acme"
    # The key is read from the *caller's* tenant path, never the default tenant's.
    assert secrets.reads == [("llm/openrouter", "acme")]
    body = fake.body
    assert body["model"] == "openai/gpt-4.1-nano"
    assert body["tools"] == [
        {
            "type": "openrouter:web_search",
            "parameters": {"max_results": 7, "max_uses": 1, "engine": "exa"},
        }
    ]
    assert body["messages"][-1] == {"role": "user", "content": "tidal turbines 2026"}
    # The deprecated mechanisms are not used.
    assert "plugins" not in body
    assert not body["model"].endswith(":online")


async def test_auto_engine_leaves_the_choice_to_openrouter() -> None:
    fake = _OpenRouter(httpx.Response(200, json=_completion()))
    search, _, _ = _search(fake, engine="auto")
    await search.search("q")
    assert "engine" not in fake.body["tools"][0]["parameters"]


# ── the answer ───────────────────────────────────────────────────────────────────


async def test_citations_become_normalised_results() -> None:
    long = "word " * 400
    fake = _OpenRouter(
        httpx.Response(
            200,
            json=_completion(
                [
                    _citation("https://a.example/x", "A page", "Alpha\n\n  text"),
                    _citation("https://a.example/x", "A page again", "dup"),
                    _citation("https://b.example/", "", long),
                    _citation("javascript:alert(1)", "evil", "x"),
                    {"type": "file_citation", "file": "x"},
                ]
            ),
        )
    )
    search, _, _ = _search(fake)

    result = await search.search("q", max_results=5)

    assert isinstance(result, WebSearchResult)
    assert result.backend == "openrouter"
    assert result.searched is True
    assert [(h.title, h.url, h.engine) for h in result.results] == [
        ("A page", "https://a.example/x", "OpenRouter"),
        # No title: the page's host stands in rather than an empty chip.
        ("b.example", "https://b.example/", "OpenRouter"),
    ]
    assert result.results[0].snippet == "Alpha text"
    # A citation's multi-kilobyte excerpt is trimmed to a preview.
    assert len(result.results[1].snippet) <= 400
    assert result.results[1].snippet.endswith("…")


def test_parse_citations_reads_the_flat_responses_shape_and_stops_at_the_limit() -> None:
    message = {
        "annotations": [
            {"type": "url_citation", "url": f"https://{n}.example", "title": str(n)}
            for n in range(5)
        ]
    }
    hits = parse_citations(message, 2)
    assert [h.url for h in hits] == ["https://0.example", "https://1.example"]


async def test_no_search_run_is_reported_as_such_not_as_empty() -> None:
    fake = _OpenRouter(httpx.Response(200, json=_completion([], searches=0)))
    search, _, _ = _search(fake)
    result = await search.search("q")
    assert result.results == []
    assert result.searched is False


async def test_a_search_that_matched_nothing_is_a_genuine_empty() -> None:
    fake = _OpenRouter(httpx.Response(200, json=_completion([], searches=1)))
    search, _, _ = _search(fake)
    result = await search.search("q")
    assert result.results == []
    assert result.searched is True


async def test_usage_is_metered_under_the_callers_tenant() -> None:
    fake = _OpenRouter(httpx.Response(200, json=_completion([_citation("https://a.example")])))
    search, _, bus = _search(
        fake, secrets=_Secrets({("acme", "llm/openrouter"): {"api_key": "sk-acme"}})
    )

    await search.search("q", tenant_id="acme")

    assert len(bus.published) == 1
    subject, payload, tenant = bus.published[0]
    assert subject == USAGE_SUBJECT
    assert tenant == "acme"
    assert payload["tenant"] == "acme"
    assert payload["model"] == "openrouter/openai/gpt-4.1-nano"
    assert payload["prompt_tokens"] == 120
    assert payload["web_search_requests"] == 1
    assert "sk-acme" not in json.dumps(payload)


# ── failures are never an empty result ───────────────────────────────────────────


async def test_no_stored_key_is_a_409_with_a_code_and_no_provider_call() -> None:
    fake = _OpenRouter(httpx.Response(200, json=_completion()))
    search, _, bus = _search(fake, secrets=_Secrets())
    with pytest.raises(WebSearchError) as err:
        await search.search("q")
    assert err.value.status == 409
    assert err.value.code == KEY_MISSING
    assert "Models page" in err.value.message
    assert fake.requests == []
    assert bus.published == []


async def test_an_unreadable_key_store_is_not_reported_as_a_missing_key() -> None:
    secrets = _Secrets()
    secrets.fail_with = SecretError("permission denied (token expired?)")
    search, _, _ = _search(_OpenRouter(httpx.Response(200, json=_completion())), secrets=secrets)
    with pytest.raises(WebSearchError) as err:
        await search.search("q")
    assert (err.value.status, err.value.code) == (503, KEY_STORE_UNAVAILABLE)


@pytest.mark.parametrize("status", [401, 403])
async def test_a_rejected_key_says_so(status: int) -> None:
    fake = _OpenRouter(
        httpx.Response(status, json={"error": {"code": status, "message": "No auth credentials"}})
    )
    search, _, _ = _search(fake)
    with pytest.raises(WebSearchError) as err:
        await search.search("q")
    assert (err.value.status, err.value.code) == (502, KEY_REJECTED)
    assert "No auth credentials" in err.value.message


async def test_a_provider_error_carries_the_providers_own_reason() -> None:
    fake = _OpenRouter(
        httpx.Response(402, json={"error": {"code": 402, "message": "Insufficient credits"}})
    )
    search, _, bus = _search(fake)
    with pytest.raises(WebSearchError) as err:
        await search.search("q")
    assert (err.value.status, err.value.code) == (502, PROVIDER_ERROR)
    assert "402" in err.value.message
    assert "Insufficient credits" in err.value.message
    assert bus.published == []


async def test_a_200_carrying_an_error_object_is_a_failure() -> None:
    fake = _OpenRouter(
        httpx.Response(200, json={"error": {"message": "Upstream provider timed out"}})
    )
    search, _, _ = _search(fake)
    with pytest.raises(WebSearchError) as err:
        await search.search("q")
    assert err.value.code == PROVIDER_ERROR
    assert "Upstream provider timed out" in err.value.message


async def test_an_unreadable_body_is_a_failure() -> None:
    search, _, _ = _search(_OpenRouter(httpx.Response(200, text="<html>oops</html>")))
    with pytest.raises(WebSearchError) as err:
        await search.search("q")
    assert err.value.code == PROVIDER_ERROR


async def test_an_unreachable_provider_is_a_502() -> None:
    search, _, _ = _search(_OpenRouter(httpx.ConnectTimeout("timed out")))
    with pytest.raises(WebSearchError) as err:
        await search.search("q")
    assert (err.value.status, err.value.code) == (502, PROVIDER_UNREACHABLE)


# ── the endpoint ─────────────────────────────────────────────────────────────────


class _FakeSearch:
    def __init__(self, *, result: WebSearchResult | None = None, error: Exception | None = None):
        self.result = result or WebSearchResult(backend="openrouter", model="m")
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def search(
        self, query: str, *, max_results: int = 5, tenant_id: str | None = None
    ) -> WebSearchResult:
        self.calls.append({"query": query, "max_results": max_results, "tenant_id": tenant_id})
        if self.error is not None:
            raise self.error
        return self.result


def _client(search: _FakeSearch | None) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_platform_router(
            CoreAppSettings(),
            object(),  # type: ignore[arg-type]  # the gateway is not touched by this route
            default_tenant="local",
            web_search=search,  # type: ignore[arg-type]
        )
    )
    return TestClient(app)


def test_endpoint_threads_the_tenant_and_returns_the_results() -> None:
    fake = _FakeSearch(
        result=WebSearchResult.model_validate(
            {
                "results": [{"title": "T", "url": "https://t.example", "engine": "OpenRouter"}],
                "searched": True,
                "backend": "openrouter",
                "model": "openai/gpt-4.1-nano",
                "search_engine": "exa",
            }
        )
    )
    resp = _client(fake).post(
        "/platform/v1/web-search", json={"query": "q", "max_results": 3, "tenant_id": "acme"}
    )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["url"] == "https://t.example"
    assert fake.calls == [{"query": "q", "max_results": 3, "tenant_id": "acme"}]


def test_endpoint_defaults_to_the_default_tenant() -> None:
    fake = _FakeSearch()
    _client(fake).post("/platform/v1/web-search", json={"query": "q"})
    assert fake.calls[0]["tenant_id"] == "local"


def test_endpoint_answers_a_missing_key_with_a_structured_409() -> None:
    fake = _FakeSearch(error=WebSearchError(409, KEY_MISSING, "No OpenRouter API key is stored."))
    resp = _client(fake).post("/platform/v1/web-search", json={"query": "q"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "code": KEY_MISSING,
        "message": "No OpenRouter API key is stored.",
        "backend": "openrouter",
    }


@pytest.mark.parametrize("body", [{"query": ""}, {"query": "q", "max_results": 0}, {}])
def test_endpoint_rejects_a_malformed_request(body: dict[str, Any]) -> None:
    assert _client(_FakeSearch()).post("/platform/v1/web-search", json=body).status_code == 422


def test_endpoint_without_a_search_backend_is_a_structured_503() -> None:
    resp = _client(None).post("/platform/v1/web-search", json={"query": "q"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "web_search_unavailable"


# ── settings ─────────────────────────────────────────────────────────────────────


def test_settings_default_to_a_cheap_model_and_a_fixed_price_engine() -> None:
    settings = CoreAppSettings()
    assert settings.openrouter_web_search_model == "openai/gpt-4.1-nano"
    assert settings.openrouter_web_search_engine == "exa"


def test_blank_settings_fall_back_to_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_WEB_SEARCH_MODEL", "  ")
    monkeypatch.setenv("OPENROUTER_WEB_SEARCH_ENGINE", "")
    settings = CoreAppSettings()
    assert settings.openrouter_web_search_model == "openai/gpt-4.1-nano"
    assert settings.openrouter_web_search_engine == "exa"


def test_settings_are_operator_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_WEB_SEARCH_MODEL", "google/gemini-2.5-flash-lite")
    monkeypatch.setenv("OPENROUTER_WEB_SEARCH_ENGINE", "Auto")
    settings = CoreAppSettings()
    assert settings.openrouter_web_search_model == "google/gemini-2.5-flash-lite"
    assert settings.openrouter_web_search_engine == "auto"


# ── the gateway's single-provider key state ──────────────────────────────────────


def _gateway(secrets: _Secrets) -> LlmGateway:
    return LlmGateway(
        ollama_url="http://localhost:11434",
        default_model="llama3.2",
        keep_alive="5m",
        power=PowerController(),
        secrets=secrets,  # type: ignore[arg-type]
        default_tenant="local",
        bus=_Bus(),  # type: ignore[arg-type]
        fallbacks=[],
    )


async def test_provider_key_state_is_tenant_scoped() -> None:
    gateway = _gateway(_Secrets({("acme", "llm/openrouter"): KEY}))
    assert await gateway.provider_key_state("openrouter", tenant_id="acme") == "present"
    assert await gateway.provider_key_state("openrouter", tenant_id="local") == "missing"
    assert await gateway.provider_key_state("local") == "not_required"
    with pytest.raises(UnknownProviderError):
        await gateway.provider_key_state("nope")


# ── the Modules-page gate on an option that needs a provider key ─────────────────


class _Prefs:
    async def enabled_map(self, tenant: str) -> dict[str, bool]:
        return {}

    async def removed_modules(self, tenant: str) -> set[str]:
        return set()

    async def get_disabled_tools(self, tenant: str, module: str) -> set[str]:
        return set()


class _ConfigSecrets:
    def __init__(self) -> None:
        self.stored: dict[str, dict[str, Any]] = {}

    async def get(self, path: str, tenant_id: str | None = None) -> dict[str, Any]:
        if path not in self.stored:
            raise SecretNotFoundError(path)
        return self.stored[path]

    async def set(self, path: str, data: dict[str, Any], tenant_id: str | None = None) -> None:
        self.stored[path] = data


def _websearch_manifest() -> ModuleManifest:
    return ModuleManifest(
        name="websearch",
        version="0.5.0",
        ui=UiSection(
            summary="web search",
            config_schema={
                "type": "object",
                "properties": {
                    "websearch_backend": {
                        "type": "string",
                        "title": "Search provider",
                        "enum": ["searxng", "openrouter"],
                        "enumRequiresProviderKey": [None, "openrouter"],
                    },
                    "websearch_max_results": {"type": "integer"},
                },
            },
        ),
    )


class _Registry(ModuleRegistry):
    async def _probe(self, base: str) -> ModuleSnapshot:
        return ModuleSnapshot(manifest=_websearch_manifest(), status=ModuleStatus(healthy=True))


def _registry(state: str | None) -> tuple[_Registry, _ConfigSecrets, list[str]]:
    asked: list[str] = []

    async def key_state(alias: str, tenant: str) -> str:
        assert tenant == "local"
        asked.append(alias)
        assert state is not None
        return state

    secrets = _ConfigSecrets()
    registry = _Registry(
        ["http://websearch:8080"],
        mcp=object(),  # type: ignore[arg-type]
        secrets=secrets,  # type: ignore[arg-type]
        tenant="local",
        prefs=_Prefs(),  # type: ignore[arg-type]
        provider_key_state=key_state if state is not None else None,
    )
    return registry, secrets, asked


async def test_choosing_openrouter_without_a_key_is_refused_and_nothing_is_saved() -> None:
    registry, secrets, asked = _registry("missing")
    with pytest.raises(HTTPException) as err:
        await registry.set_config("websearch", {"websearch_backend": "openrouter"})
    assert err.value.status_code == 409
    detail = err.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "provider_key_required"
    assert detail["provider"] == "openrouter"
    assert "Models page" in detail["message"]
    assert asked == ["openrouter"]
    assert secrets.stored == {}


async def test_choosing_openrouter_with_a_key_saves() -> None:
    registry, secrets, _ = _registry("present")
    await registry.set_config("websearch", {"websearch_backend": "openrouter"})
    assert secrets.stored["modules/websearch/config"] == {"websearch_backend": "openrouter"}


async def test_an_unreadable_key_store_refuses_with_503_not_a_false_missing() -> None:
    registry, _, _ = _registry("unavailable")
    with pytest.raises(HTTPException) as err:
        await registry.set_config("websearch", {"websearch_backend": "openrouter"})
    assert err.value.status_code == 503
    assert isinstance(err.value.detail, dict)
    assert err.value.detail["code"] == "key_store_unavailable"


async def test_an_option_that_needs_nothing_never_asks_for_a_key() -> None:
    registry, secrets, asked = _registry("missing")
    await registry.set_config(
        "websearch", {"websearch_backend": "searxng", "websearch_max_results": 3}
    )
    assert asked == []
    assert secrets.stored["modules/websearch/config"]["websearch_backend"] == "searxng"


async def test_without_a_key_lookup_the_gate_is_skipped() -> None:
    registry, secrets, _ = _registry(None)
    await registry.set_config("websearch", {"websearch_backend": "openrouter"})
    assert "modules/websearch/config" in secrets.stored


async def test_an_unknown_provider_alias_is_a_409_refusal_not_a_500() -> None:
    async def key_state(alias: str, tenant: str) -> str:
        raise UnknownProviderError(f"no provider named {alias!r}")

    registry = _Registry(
        ["http://websearch:8080"],
        mcp=object(),  # type: ignore[arg-type]
        secrets=_ConfigSecrets(),  # type: ignore[arg-type]
        tenant="local",
        prefs=_Prefs(),  # type: ignore[arg-type]
        provider_key_state=key_state,
    )
    with pytest.raises(HTTPException) as err:
        await registry.set_config("websearch", {"websearch_backend": "openrouter"})
    assert err.value.status_code == 409
    assert isinstance(err.value.detail, dict)
    assert err.value.detail["code"] == "provider_key_required"


async def test_the_key_gate_asks_for_the_registrys_own_tenant() -> None:
    tenants: list[str] = []

    async def key_state(alias: str, tenant: str) -> str:
        tenants.append(tenant)
        return "present"

    registry = _Registry(
        ["http://websearch:8080"],
        mcp=object(),  # type: ignore[arg-type]
        secrets=_ConfigSecrets(),  # type: ignore[arg-type]
        tenant="acme",
        prefs=_Prefs(),  # type: ignore[arg-type]
        provider_key_state=key_state,
    )
    await registry.set_config("websearch", {"websearch_backend": "openrouter"})
    assert tenants == ["acme"]
