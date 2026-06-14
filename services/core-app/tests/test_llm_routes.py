"""Tests for the LLM gateway router after the chat-surface cleanup (#114)."""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from epicurus_core_app.llm.routes import create_llm_router


class _StubGateway:
    """Only needs to exist — these tests inspect routes, not call behavior."""


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(create_llm_router(_StubGateway()))  # type: ignore[arg-type]
    return app


def test_llm_chat_endpoint_is_removed() -> None:
    # Folded into POST /platform/v1/chat (ADR-0021); the gateway no longer serves it.
    assert "/platform/v1/llm/chat" not in _app().openapi()["paths"]


def test_management_routes_remain() -> None:
    paths = _app().openapi()["paths"]
    assert "/platform/v1/llm/models" in paths
    assert "/platform/v1/llm/providers" in paths
    assert "/platform/v1/llm/pull" in paths


async def test_llm_chat_post_returns_404() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/platform/v1/llm/chat", json={"messages": [{"role": "user", "content": "hi"}]}
        )
    assert resp.status_code == 404
