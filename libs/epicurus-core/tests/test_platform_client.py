"""Tests for PlatformClient — httpx calls are monkeypatched (no network)."""

from __future__ import annotations

from typing import Any

import pytest

from epicurus_core import PlatformClient, PlatformMessage
from epicurus_core.platform_client import PlatformChatResponse

# ── helpers ────────────────────────────────────────────────────────────────────


class _Resp:
    """Minimal fake httpx.Response."""

    def __init__(self, *, body: dict[str, Any], status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._body


class _HttpClient:
    """Fake httpx.AsyncClient context manager that captures the last POST call."""

    def __init__(self, *, response: _Resp, capture: dict[str, Any]) -> None:
        self._response = response
        self._capture = capture

    async def __aenter__(self) -> _HttpClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def post(self, url: str, *, json: Any) -> _Resp:
        self._capture["url"] = url
        self._capture["body"] = json
        return self._response

    async def get(self, url: str, *, params: Any = None) -> _Resp:
        self._capture["url"] = url
        self._capture["params"] = params
        return self._response


# ── embed ──────────────────────────────────────────────────────────────────────


async def test_embed_sends_texts_and_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    resp = _Resp(body={"embeddings": [[0.1, 0.2], [0.3, 0.4]]})

    monkeypatch.setattr(
        "epicurus_core.platform_client.httpx.AsyncClient",
        lambda *a, **kw: _HttpClient(response=resp, capture=captured),
    )

    client = PlatformClient(base_url="http://core:8080", tenant_id="local")
    result = await client.embed(["hello", "world"])

    assert result == [[0.1, 0.2], [0.3, 0.4]]
    assert captured["url"] == "/platform/v1/embed"
    assert captured["body"]["texts"] == ["hello", "world"]
    assert captured["body"]["tenant_id"] == "local"
    assert "model" not in captured["body"]


async def test_embed_includes_explicit_model(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    resp = _Resp(body={"embeddings": [[0.5]]})

    monkeypatch.setattr(
        "epicurus_core.platform_client.httpx.AsyncClient",
        lambda *a, **kw: _HttpClient(response=resp, capture=captured),
    )

    await PlatformClient(base_url="http://core:8080", tenant_id="local").embed(
        ["hi"], model="mxbai-embed-large"
    )
    assert captured["body"]["model"] == "mxbai-embed-large"


async def test_embed_strips_trailing_slash_from_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    resp = _Resp(body={"embeddings": [[0.0]]})

    def _make_client(*a: Any, **kw: Any) -> _HttpClient:
        captured["base_url"] = kw.get("base_url", a[0] if a else "")
        return _HttpClient(response=resp, capture=captured)

    monkeypatch.setattr("epicurus_core.platform_client.httpx.AsyncClient", _make_client)

    await PlatformClient(base_url="http://core:8080/", tenant_id="local").embed(["x"])
    assert captured["base_url"] == "http://core:8080"


# ── chat ───────────────────────────────────────────────────────────────────────


async def test_chat_sends_messages_and_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    resp = _Resp(
        body={
            "model": "ollama_chat/llama3.2",
            "content": "hi there",
            "tool_calls": None,
            "prompt_tokens": 4,
            "completion_tokens": 3,
        }
    )

    monkeypatch.setattr(
        "epicurus_core.platform_client.httpx.AsyncClient",
        lambda *a, **kw: _HttpClient(response=resp, capture=captured),
    )

    client = PlatformClient(base_url="http://core:8080", tenant_id="workspace-1")
    result = await client.chat([PlatformMessage(role="user", content="hello")])

    assert isinstance(result, PlatformChatResponse)
    assert result.content == "hi there"
    assert result.model == "ollama_chat/llama3.2"
    assert result.prompt_tokens == 4
    assert captured["url"] == "/platform/v1/chat"
    assert captured["body"]["tenant_id"] == "workspace-1"
    assert captured["body"]["messages"] == [{"role": "user", "content": "hello"}]
    assert "model" not in captured["body"]
    assert "tools" not in captured["body"]


async def test_chat_includes_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    resp = _Resp(body={"model": "m", "content": "ok", "tool_calls": None})

    monkeypatch.setattr(
        "epicurus_core.platform_client.httpx.AsyncClient",
        lambda *a, **kw: _HttpClient(response=resp, capture=captured),
    )

    await PlatformClient(base_url="http://core:8080", tenant_id="local").chat(
        [PlatformMessage(role="user", content="hi")],
        model="claude/claude-3-5-sonnet-latest",
    )
    assert captured["body"]["model"] == "claude/claude-3-5-sonnet-latest"


async def test_chat_includes_tools_when_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    resp = _Resp(body={"model": "m", "content": "done", "tool_calls": None})

    monkeypatch.setattr(
        "epicurus_core.platform_client.httpx.AsyncClient",
        lambda *a, **kw: _HttpClient(response=resp, capture=captured),
    )

    tools = [{"type": "function", "function": {"name": "search"}}]
    await PlatformClient(base_url="http://core:8080", tenant_id="local").chat(
        [PlatformMessage(role="user", content="find it")], tools=tools
    )
    assert captured["body"]["tools"] == tools


async def test_chat_omits_none_fields_from_message(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    resp = _Resp(body={"model": "m", "content": "ok", "tool_calls": None})

    monkeypatch.setattr(
        "epicurus_core.platform_client.httpx.AsyncClient",
        lambda *a, **kw: _HttpClient(response=resp, capture=captured),
    )

    await PlatformClient(base_url="http://core:8080", tenant_id="local").chat(
        [PlatformMessage(role="user", content="hi")]
    )
    msg = captured["body"]["messages"][0]
    # None fields must not appear in the serialised message
    assert "tool_calls" not in msg
    assert "tool_call_id" not in msg
    assert "name" not in msg


# ── get_module_model (#128) ──────────────────────────────────────────────────────


async def test_get_module_model_returns_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    resp = _Resp(body={"model": "nomic-embed-text"})
    monkeypatch.setattr(
        "epicurus_core.platform_client.httpx.AsyncClient",
        lambda *a, **kw: _HttpClient(response=resp, capture=captured),
    )
    client = PlatformClient(base_url="http://core:8080", tenant_id="local", module="knowledge")
    model = await client.get_module_model("embedding")
    assert model == "nomic-embed-text"
    assert captured["url"] == "/platform/v1/modules/knowledge/models/embedding"
    assert captured["params"] == {"tenant_id": "local"}


async def test_get_module_model_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _Resp(body={"model": None})
    monkeypatch.setattr(
        "epicurus_core.platform_client.httpx.AsyncClient",
        lambda *a, **kw: _HttpClient(response=resp, capture={}),
    )
    client = PlatformClient(base_url="http://core:8080", tenant_id="local", module="knowledge")
    assert await client.get_module_model("embedding") is None


async def test_get_module_model_requires_module_name() -> None:
    client = PlatformClient(base_url="http://core:8080", tenant_id="local")
    with pytest.raises(ValueError, match="module must be set"):
        await client.get_module_model("embedding")


# ── get_collections (ADR-0030) ────────────────────────────────────────────────────


async def test_get_collections_parses_prefs(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    resp = _Resp(
        body={
            "enabled": [{"account": "google", "collection": "primary"}],
            "active": {"account": "google", "collection": "primary"},
        }
    )
    monkeypatch.setattr(
        "epicurus_core.platform_client.httpx.AsyncClient",
        lambda *a, **kw: _HttpClient(response=resp, capture=captured),
    )
    client = PlatformClient(base_url="http://core:8080", tenant_id="local", module="calendar")
    prefs = await client.get_collections()
    assert captured["url"] == "/platform/v1/modules/calendar/collections/prefs"
    assert captured["params"] == {"tenant_id": "local"}
    assert prefs.active is not None
    assert prefs.active.collection == "primary"
    assert prefs.enabled[0].account == "google"


async def test_get_collections_empty_means_local(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _Resp(body={"enabled": [], "active": None})
    monkeypatch.setattr(
        "epicurus_core.platform_client.httpx.AsyncClient",
        lambda *a, **kw: _HttpClient(response=resp, capture={}),
    )
    client = PlatformClient(base_url="http://core:8080", tenant_id="local", module="calendar")
    prefs = await client.get_collections()
    assert prefs.enabled == []
    assert prefs.active is None


async def test_get_collections_requires_module_name() -> None:
    client = PlatformClient(base_url="http://core:8080", tenant_id="local")
    with pytest.raises(ValueError, match="module must be set"):
        await client.get_collections()
