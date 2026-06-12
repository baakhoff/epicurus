"""Tests for the /platform/v1 embed and chat endpoints.

The LLM gateway is replaced by a lightweight fake so no network is needed.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from epicurus_core_app.llm.models import ChatMessage, ChatResult
from epicurus_core_app.llm.power import GatewayPausedError
from epicurus_core_app.platform_api import create_platform_router
from epicurus_core_app.settings import CoreAppSettings


class _FakeGateway:
    """A stand-in for LlmGateway that records calls and returns seeded values."""

    def __init__(
        self,
        *,
        embed_result: list[list[float]] | None = None,
        chat_result: ChatResult | None = None,
        raise_on_chat: Exception | None = None,
    ) -> None:
        self._embed_result = embed_result or [[0.1, 0.2]]
        self._chat_result = chat_result or ChatResult(model="test/m", content="ok")
        self._raise_on_chat = raise_on_chat
        self.embed_calls: list[dict[str, Any]] = []
        self.chat_calls: list[dict[str, Any]] = []

    async def embed(
        self, texts: list[str], *, model: str | None = None, tenant_id: str | None = None
    ) -> list[list[float]]:
        self.embed_calls.append({"texts": texts, "model": model, "tenant_id": tenant_id})
        return self._embed_result

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tenant_id: str | None = None,
    ) -> ChatResult:
        self.chat_calls.append(
            {"messages": messages, "model": model, "tools": tools, "tenant_id": tenant_id}
        )
        if self._raise_on_chat is not None:
            raise self._raise_on_chat
        return self._chat_result


def _settings(*, embed_model: str = "nomic-embed-text") -> CoreAppSettings:
    return CoreAppSettings(
        service_name="test",
        memory_embed_model=embed_model,
    )


def _app(gw: _FakeGateway, *, embed_model: str = "nomic-embed-text") -> FastAPI:
    app = FastAPI()
    app.include_router(create_platform_router(_settings(embed_model=embed_model), gw))  # type: ignore[arg-type]

    @app.exception_handler(GatewayPausedError)
    async def _on_paused(_request: Request, exc: GatewayPausedError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    return app


# ── /info ──────────────────────────────────────────────────────────────────────


async def test_info_returns_contract_and_version() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_FakeGateway())), base_url="http://test"
    ) as client:
        resp = await client.get("/platform/v1/info")
    assert resp.status_code == 200
    body = resp.json()
    assert body["contract_version"] == "0.1"
    assert "core_version" in body


# ── /embed ─────────────────────────────────────────────────────────────────────


async def test_embed_returns_vectors_for_texts() -> None:
    gw = _FakeGateway(embed_result=[[0.1, 0.2], [0.3, 0.4]])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        resp = await client.post("/platform/v1/embed", json={"texts": ["hello", "world"]})
    assert resp.status_code == 200
    assert resp.json()["embeddings"] == [[0.1, 0.2], [0.3, 0.4]]


async def test_embed_uses_configured_default_model() -> None:
    gw = _FakeGateway(embed_result=[[0.0]])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw, embed_model="nomic-embed-text")),
        base_url="http://test",
    ) as client:
        await client.post("/platform/v1/embed", json={"texts": ["hi"]})
    assert gw.embed_calls[0]["model"] == "nomic-embed-text"


async def test_embed_uses_explicit_model_when_provided() -> None:
    gw = _FakeGateway(embed_result=[[0.0]])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        await client.post(
            "/platform/v1/embed",
            json={"texts": ["hi"], "model": "mxbai-embed-large"},
        )
    assert gw.embed_calls[0]["model"] == "mxbai-embed-large"


async def test_embed_passes_all_texts_to_gateway() -> None:
    gw = _FakeGateway(embed_result=[[0.0], [0.0], [0.0]])
    texts = ["a", "b", "c"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        await client.post("/platform/v1/embed", json={"texts": texts})
    assert gw.embed_calls[0]["texts"] == texts


# ── /chat ──────────────────────────────────────────────────────────────────────


async def test_chat_returns_content_and_model() -> None:
    gw = _FakeGateway(
        chat_result=ChatResult(
            model="ollama_chat/llama3.2",
            content="hello back",
            prompt_tokens=3,
            completion_tokens=5,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/platform/v1/chat",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "hello back"
    assert body["model"] == "ollama_chat/llama3.2"
    assert body["prompt_tokens"] == 3
    assert body["completion_tokens"] == 5


async def test_chat_forwards_model_override() -> None:
    gw = _FakeGateway()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        await client.post(
            "/platform/v1/chat",
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "model": "claude/claude-3-5-sonnet-latest",
            },
        )
    assert gw.chat_calls[0]["model"] == "claude/claude-3-5-sonnet-latest"


async def test_chat_forwards_tools_and_tenant() -> None:
    gw = _FakeGateway()
    tools = [{"type": "function", "function": {"name": "lookup"}}]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        await client.post(
            "/platform/v1/chat",
            json={
                "messages": [{"role": "user", "content": "find it"}],
                "tools": tools,
                "tenant_id": "workspace-1",
            },
        )
    call = gw.chat_calls[0]
    assert call["tools"] == tools
    assert call["tenant_id"] == "workspace-1"


async def test_chat_returns_tool_calls_when_present() -> None:
    tc = [{"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]
    gw = _FakeGateway(chat_result=ChatResult(model="m", content="", tool_calls=tc))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/platform/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.json()["tool_calls"] == tc


async def test_paused_gateway_returns_503() -> None:
    gw = _FakeGateway(raise_on_chat=GatewayPausedError("paused"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/platform/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 503
    assert "paused" in resp.json()["detail"]
