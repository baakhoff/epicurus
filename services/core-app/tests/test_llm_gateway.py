"""Unit tests for the LLM gateway — LiteLLM, Ollama, and OpenBao are mocked (no network)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from structlog.testing import capture_logs

from epicurus_core import SecretError
from epicurus_core_app.llm.gateway import LlmGateway
from epicurus_core_app.llm.models import ChatMessage, PowerState
from epicurus_core_app.llm.power import GatewayPausedError, PowerController


class _FakeSecrets:
    """A stand-in for SecretStore: returns seeded secrets, else raises SecretError."""

    def __init__(self, data: dict[str, dict[str, Any]] | None = None) -> None:
        self._data = data or {}

    async def get(self, path: str, tenant_id: str | None = None) -> dict[str, Any]:
        if path in self._data:
            return self._data[path]
        raise SecretError(f"not found: {path}")


class _FakeBus:
    """A stand-in for EventBus that records published events."""

    def __init__(self) -> None:
        self.published: list[tuple[str, Any, str | None]] = []

    async def publish(self, subject: str, data: Any, tenant_id: str | None = None) -> None:
        self.published.append((subject, data, tenant_id))


def _gateway(
    power: PowerController | None = None,
    secrets: Any = None,
    bus: Any = None,
    fallbacks: list[str] | None = None,
) -> LlmGateway:
    return LlmGateway(
        ollama_url="http://ollama:11434",
        default_model="llama3.2",
        keep_alive="5m",
        power=power or PowerController(),
        secrets=secrets or _FakeSecrets(),
        default_tenant="local",
        bus=bus or _FakeBus(),
        fallbacks=fallbacks or [],
        num_retries=2,
    )


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, Any]:
        return self._payload


async def test_chat_prefixes_model_and_extracts_content(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(
            {
                "model": "ollama_chat/llama3.2",
                "choices": [{"message": {"content": "hi there", "tool_calls": None}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    result = await _gateway().chat([ChatMessage(role="user", content="hi")])

    assert captured["model"] == "ollama_chat/llama3.2"
    assert captured["api_base"] == "http://ollama:11434"
    assert captured["keep_alive"] == "5m"
    assert result.content == "hi there"
    assert result.completion_tokens == 2


async def test_chat_uses_explicit_model(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"model": "x", "choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    await _gateway().chat([ChatMessage(role="user", content="hi")], model="qwen2.5:0.5b")
    assert captured["model"] == "ollama_chat/qwen2.5:0.5b"


async def test_hosted_chat_fetches_key_and_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"model": "anthropic/c", "choices": [{"message": {"content": "hey"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    secrets = _FakeSecrets({"llm/anthropic": {"api_key": "fixture-anthropic"}})
    result = await _gateway(secrets=secrets).chat(
        [ChatMessage(role="user", content="hi")], model="claude/claude-3-5-sonnet-latest"
    )

    assert captured["model"] == "anthropic/claude-3-5-sonnet-latest"
    assert captured["api_key"] == "fixture-anthropic"
    assert "api_base" not in captured  # hosted Anthropic uses its own endpoint
    assert "keep_alive" not in captured  # only the local runtime gets keep_alive
    assert result.content == "hey"


async def test_custom_provider_uses_base_url_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"model": "openai/m", "choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    secrets = _FakeSecrets({"llm/custom": {"api_key": "k", "api_base": "http://my-llm:8000/v1"}})
    await _gateway(secrets=secrets).chat([ChatMessage(role="user", content="hi")], model="custom/m")

    assert captured["model"] == "openai/m"
    assert captured["api_key"] == "k"
    assert captured["api_base"] == "http://my-llm:8000/v1"


async def test_api_key_is_not_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**kwargs: Any) -> _Response:
        return _Response({"model": "anthropic/c", "choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    secrets = _FakeSecrets({"llm/anthropic": {"api_key": "fixture-redaction-sentinel"}})
    with capture_logs() as logs:
        await _gateway(secrets=secrets).chat(
            [ChatMessage(role="user", content="hi")], model="claude/c"
        )
    assert not any("fixture-redaction-sentinel" in str(entry) for entry in logs)


async def test_providers_reports_configured() -> None:
    secrets = _FakeSecrets({"llm/anthropic": {"api_key": "k"}})
    infos = {p.alias: p for p in await _gateway(secrets=secrets).providers()}
    assert infos["local"].local and infos["local"].configured
    assert infos["claude"].configured  # key seeded
    assert not infos["gpt"].configured  # no key


async def test_falls_back_when_primary_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_acompletion(**kwargs: Any) -> _Response:
        calls.append(kwargs["model"])
        if kwargs["model"].startswith("ollama_chat/"):
            raise RuntimeError("local is down")
        return _Response(
            {"model": kwargs["model"], "choices": [{"message": {"content": "from fallback"}}]}
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    secrets = _FakeSecrets({"llm/anthropic": {"api_key": "k"}})
    gw = _gateway(secrets=secrets, fallbacks=["claude/claude-3-5-sonnet-latest"])
    result = await gw.chat([ChatMessage(role="user", content="hi")])

    assert calls == ["ollama_chat/llama3.2", "anthropic/claude-3-5-sonnet-latest"]
    assert result.content == "from fallback"


async def test_paused_skips_local_and_uses_hosted_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_acompletion(**kwargs: Any) -> _Response:
        calls.append(kwargs["model"])
        return _Response({"model": kwargs["model"], "choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    power = PowerController()
    power.pause()
    secrets = _FakeSecrets({"llm/anthropic": {"api_key": "k"}})
    gw = _gateway(power=power, secrets=secrets, fallbacks=["claude/claude-3-5-sonnet-latest"])
    result = await gw.chat([ChatMessage(role="user", content="hi")])

    assert calls == ["anthropic/claude-3-5-sonnet-latest"]  # local primary was skipped
    assert result.content == "ok"


async def test_usage_event_emitted_without_key_or_content(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**kwargs: Any) -> _Response:
        return _Response(
            {
                "model": "ollama_chat/llama3.2",
                "choices": [{"message": {"content": "secret-reply"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7},
            }
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    bus = _FakeBus()
    await _gateway(bus=bus).chat([ChatMessage(role="user", content="hi")])

    assert len(bus.published) == 1
    subject, data, tenant = bus.published[0]
    assert subject == "llm.usage"
    assert tenant == "local"
    assert data["model"] == "ollama_chat/llama3.2"
    assert data["completion_tokens"] == 7
    assert "api_key" not in data
    assert "secret-reply" not in str(data)  # no prompt/response content in the event


async def test_num_retries_passed_to_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(
            {"model": "ollama_chat/llama3.2", "choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    await _gateway().chat([ChatMessage(role="user", content="hi")])
    assert captured["num_retries"] == 2


async def test_embed_refuses_when_paused() -> None:
    power = PowerController()
    power.pause()
    with pytest.raises(GatewayPausedError):
        await _gateway(power).embed(["text"])


async def test_paused_gateway_refuses() -> None:
    power = PowerController()
    power.pause()
    with pytest.raises(GatewayPausedError):
        await _gateway(power).chat([ChatMessage(role="user", content="hi")])
    assert power.state is PowerState.PAUSED


async def test_stream_yields_content_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Delta:
        def __init__(self, content: str | None) -> None:
            self.content = content

    class _Choice:
        def __init__(self, content: str | None) -> None:
            self.delta = _Delta(content)

    class _Chunk:
        def __init__(self, content: str | None) -> None:
            self.choices = [_Choice(content)]

    async def fake_chunks() -> AsyncIterator[_Chunk]:
        for piece in ["he", "llo", ""]:
            yield _Chunk(piece)

    async def fake_acompletion(**kwargs: Any) -> AsyncIterator[_Chunk]:
        return fake_chunks()

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    pieces = [p async for p in _gateway().stream([ChatMessage(role="user", content="hi")])]
    assert pieces == ["he", "llo"]


async def test_models_lists_from_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    class _HttpResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"models": [{"name": "llama3.2", "size": 42}]}

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, path: str) -> _HttpResponse:
            return _HttpResponse()

    monkeypatch.setattr("epicurus_core_app.llm.gateway.httpx.AsyncClient", _Client)
    models = await _gateway().models()
    assert models[0].name == "llama3.2"
    assert models[0].size == 42


async def test_stream_chat_assembles_tool_call_fragments(monkeypatch: pytest.MonkeyPatch) -> None:
    # A streamed tool call arrives in fragments: the name in one chunk, then the JSON
    # arguments split across two more. stream_chat must coalesce them by index.
    class _Fn:
        def __init__(self, name: str | None = None, arguments: str | None = None) -> None:
            self.name = name
            self.arguments = arguments

    class _Fragment:
        def __init__(
            self,
            index: int,
            call_id: str | None = None,
            name: str | None = None,
            arguments: str | None = None,
        ) -> None:
            self.index = index
            self.id = call_id
            self.function = _Fn(name, arguments)

    class _Delta:
        def __init__(
            self, content: str | None = None, tool_calls: list[_Fragment] | None = None
        ) -> None:
            self.content = content
            self.tool_calls = tool_calls

    class _Choice:
        def __init__(self, delta: _Delta) -> None:
            self.delta = delta

    class _Chunk:
        def __init__(self, delta: _Delta) -> None:
            self.choices = [_Choice(delta)]

    async def fake_chunks() -> AsyncIterator[_Chunk]:
        yield _Chunk(_Delta(content="on it"))
        yield _Chunk(_Delta(tool_calls=[_Fragment(0, call_id="call_1", name="echo")]))
        yield _Chunk(_Delta(tool_calls=[_Fragment(0, arguments='{"mess')]))
        yield _Chunk(_Delta(tool_calls=[_Fragment(0, arguments='age": "hi"}')]))

    async def fake_acompletion(**kwargs: Any) -> AsyncIterator[_Chunk]:
        return fake_chunks()

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    events = [
        event
        async for event in _gateway().stream_chat(
            [ChatMessage(role="user", content="echo hi")],
            tools=[{"type": "function", "function": {"name": "echo"}}],
        )
    ]

    assert [e.delta for e in events if e.delta] == ["on it"]
    results = [e.result for e in events if e.result is not None]
    assert len(results) == 1
    call = (results[0].tool_calls or [])[0]
    assert call["id"] == "call_1"
    assert call["function"]["name"] == "echo"
    # the two argument fragments were concatenated into valid JSON
    assert call["function"]["arguments"] == '{"message": "hi"}'
