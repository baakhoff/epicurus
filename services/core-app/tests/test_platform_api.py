"""Tests for the /platform/v1 embed and chat endpoints.

The LLM gateway is replaced by a lightweight fake so no network is needed.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.llm.models import ChatMessage, ChatResult
from epicurus_core_app.llm.power import GatewayPausedError
from epicurus_core_app.llm.prefs import LlmPrefsStore
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
        vision: bool = True,
        default_model: str = "test/default",
        raise_on_default: Exception | None = None,
    ) -> None:
        self._embed_result = embed_result or [[0.1, 0.2]]
        self._chat_result = chat_result or ChatResult(model="test/m", content="ok")
        self._raise_on_chat = raise_on_chat
        self._vision = vision
        self._default_model = default_model
        self._raise_on_default = raise_on_default
        self.embed_calls: list[dict[str, Any]] = []
        self.chat_calls: list[dict[str, Any]] = []
        self.vision_calls: list[tuple[str | None, str | None]] = []

    async def supports_vision(self, model: str | None = None, tenant_id: str | None = None) -> bool:
        self.vision_calls.append((model, tenant_id))
        return self._vision

    async def effective_default(self, tenant_id: str | None = None) -> str:
        if self._raise_on_default is not None:
            raise self._raise_on_default
        return self._default_model

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


async def _fresh_prefs() -> LlmPrefsStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = LlmPrefsStore(engine)
    await store.init()
    return store


def _app(
    gw: _FakeGateway,
    *,
    embed_model: str = "nomic-embed-text",
    prefs: LlmPrefsStore | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(  # type: ignore[arg-type]
        create_platform_router(_settings(embed_model=embed_model), gw, prefs=prefs)
    )

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


async def test_embed_uses_global_embed_default_when_no_model_given() -> None:
    prefs = await _fresh_prefs()
    await prefs.set_embed_default("local", "mxbai-embed-large")
    gw = _FakeGateway(embed_result=[[0.0]])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw, prefs=prefs)), base_url="http://test"
    ) as client:
        await client.post("/platform/v1/embed", json={"texts": ["hi"]})
    assert gw.embed_calls[0]["model"] == "mxbai-embed-large"


async def test_embed_per_module_override_wins_over_global_embed_default() -> None:
    prefs = await _fresh_prefs()
    await prefs.set_embed_default("local", "mxbai-embed-large")
    gw = _FakeGateway(embed_result=[[0.0]])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw, prefs=prefs)), base_url="http://test"
    ) as client:
        await client.post(
            "/platform/v1/embed",
            json={"texts": ["hi"], "model": "nomic-embed-text"},
        )
    assert gw.embed_calls[0]["model"] == "nomic-embed-text"


async def test_embed_falls_back_to_env_default_when_global_embed_default_unset() -> None:
    prefs = await _fresh_prefs()
    gw = _FakeGateway(embed_result=[[0.0]])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw, embed_model="nomic-embed-text", prefs=prefs)),
        base_url="http://test",
    ) as client:
        await client.post("/platform/v1/embed", json={"texts": ["hi"]})
    assert gw.embed_calls[0]["model"] == "nomic-embed-text"


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


# ── /chat with images — the vision gate (#739) ─────────────────────────────────────────
#
# Until #739 this endpoint had no vision gate at all: the interactive agent turn checked
# `supports_vision` (#633) but a *module* sending image content-parts got either a silent
# ignore or a raw provider error. These pin the gate, its structured body (a module branches
# on `detail.error`), and — just as important — that a text-only request is untouched.


def _image_message(text: str = "describe this") -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }


async def test_image_request_reaches_a_vision_capable_model() -> None:
    gw = _FakeGateway(vision=True, chat_result=ChatResult(model="v/m", content="a red bike"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        resp = await client.post("/platform/v1/chat", json={"messages": [_image_message()]})
    assert resp.status_code == 200
    assert resp.json()["content"] == "a red bike"
    assert len(gw.chat_calls) == 1
    # The parts array survives the round trip into the gateway unflattened.
    content = gw.chat_calls[0]["messages"][0].content
    assert isinstance(content, list)
    assert content[1]["type"] == "image_url"


async def test_image_request_to_a_blind_model_is_refused_with_400() -> None:
    gw = _FakeGateway(vision=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        resp = await client.post("/platform/v1/chat", json={"messages": [_image_message()]})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error"] == "unsupported_media"
    assert "vision-capable model" in detail["message"]


async def test_the_refusal_happens_before_any_provider_call() -> None:
    """The whole point: never a mangled attempt, never a provider 500."""
    gw = _FakeGateway(vision=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        await client.post("/platform/v1/chat", json={"messages": [_image_message()]})
    assert gw.chat_calls == []


async def test_the_refusal_names_the_requested_model() -> None:
    gw = _FakeGateway(vision=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/platform/v1/chat",
            json={"messages": [_image_message()], "model": "ollama_chat/llama3.2"},
        )
    assert resp.json()["detail"]["model"] == "ollama_chat/llama3.2"


async def test_the_refusal_names_the_core_default_when_no_override_was_sent() -> None:
    gw = _FakeGateway(vision=False, default_model="ollama_chat/qwen3")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        resp = await client.post("/platform/v1/chat", json={"messages": [_image_message()]})
    assert resp.json()["detail"]["model"] == "ollama_chat/qwen3"


async def test_a_broken_default_lookup_still_yields_a_clean_400() -> None:
    """A store hiccup on the refusal path must not turn a 400 into a 500."""
    gw = _FakeGateway(vision=False, raise_on_default=RuntimeError("prefs store down"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        resp = await client.post("/platform/v1/chat", json={"messages": [_image_message()]})
    assert resp.status_code == 400
    assert resp.json()["detail"]["model"] == ""


async def test_the_capability_check_is_scoped_to_the_request_model_and_tenant() -> None:
    gw = _FakeGateway(vision=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        await client.post(
            "/platform/v1/chat",
            json={
                "messages": [_image_message()],
                "model": "some/model",
                "tenant_id": "workspace-1",
            },
        )
    assert gw.vision_calls == [("some/model", "workspace-1")]


async def test_a_text_only_request_never_consults_the_vision_capability() -> None:
    """No extra capability lookup on the common path — the gate is image-triggered only."""
    gw = _FakeGateway(vision=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/platform/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}
        )
    assert resp.status_code == 200
    assert gw.vision_calls == []


async def test_a_text_only_parts_array_is_not_treated_as_an_image() -> None:
    gw = _FakeGateway(vision=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/platform/v1/chat",
            json={
                "messages": [{"role": "user", "content": [{"type": "text", "text": "just words"}]}]
            },
        )
    assert resp.status_code == 200


async def test_the_anthropic_native_image_part_is_gated_too() -> None:
    """The gate keys on both spellings, so it cannot be sidestepped by choosing the other."""
    gw = _FakeGateway(vision=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/platform/v1/chat",
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "data": "AAAA"}}
                        ],
                    }
                ]
            },
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "unsupported_media"


async def test_an_image_anywhere_in_the_history_triggers_the_gate() -> None:
    """A multi-turn module conversation carrying an earlier image is still an image request."""
    gw = _FakeGateway(vision=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(gw)), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/platform/v1/chat",
            json={
                "messages": [
                    _image_message("first look"),
                    {"role": "assistant", "content": "a bicycle"},
                    {"role": "user", "content": "and the background?"},
                ]
            },
        )
    assert resp.status_code == 400
