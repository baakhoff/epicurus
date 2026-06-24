"""Unit tests for the LLM gateway — LiteLLM, Ollama, and OpenBao are mocked (no network)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from structlog.testing import capture_logs

from epicurus_core import SecretError
from epicurus_core_app.llm.gateway import LlmGateway
from epicurus_core_app.llm.model_settings import ModelSettings, ModelSettingsStore
from epicurus_core_app.llm.models import ChatMessage, ModelInfo, PowerState
from epicurus_core_app.llm.power import GatewayPausedError, PowerController
from epicurus_core_app.llm.prefs import LlmPrefsStore


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


async def _fresh_prefs() -> LlmPrefsStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    prefs = LlmPrefsStore(engine)
    await prefs.init()
    return prefs


async def _fresh_model_settings() -> ModelSettingsStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = ModelSettingsStore(engine)
    await store.init()
    return store


def _gateway(
    power: PowerController | None = None,
    secrets: Any = None,
    bus: Any = None,
    fallbacks: list[str] | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    num_ctx: int | None = None,
    prefs: LlmPrefsStore | None = None,
    model_settings: ModelSettingsStore | None = None,
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
        temperature=temperature,
        top_p=top_p,
        num_ctx=num_ctx,
        prefs=prefs,
        model_settings=model_settings,
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


async def test_embed_emits_usage_event(monkeypatch: pytest.MonkeyPatch) -> None:
    class _EmbedResp:
        def model_dump(self) -> dict[str, Any]:
            return {"data": [{"embedding": [0.1, 0.2]}]}

    async def fake_aembedding(**kwargs: Any) -> _EmbedResp:
        return _EmbedResp()

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", fake_aembedding)
    bus = _FakeBus()
    await _gateway(bus=bus).embed(["hello"])

    assert len(bus.published) == 1
    subject, data, tenant = bus.published[0]
    assert subject == "llm.usage"
    assert tenant == "local"
    assert data["model"].startswith("ollama/")
    assert "api_key" not in data


async def test_embed_usage_event_is_tenant_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    # A module's embed call meters under that module's tenant, not the global default
    # (ADR-0002: no single-global-tenant code paths, even at one tenant).
    class _EmbedResp:
        def model_dump(self) -> dict[str, Any]:
            return {"data": [{"embedding": [0.1, 0.2]}]}

    async def fake_aembedding(**kwargs: Any) -> _EmbedResp:
        return _EmbedResp()

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", fake_aembedding)
    bus = _FakeBus()
    await _gateway(bus=bus).embed(["hi"], tenant_id="tenant-x")

    _subject, _data, tenant = bus.published[0]
    assert tenant == "tenant-x"


async def test_embed_resolves_global_embed_default_pref(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's UI Embedding-model choice (embed_default pref) drives embedding.

    Before this, memory embedding ignored the pref and always hit the env default — a 404
    when that model wasn't pulled. embed() with no explicit model now resolves the pref,
    falling back to the env default; an explicit per-module model still wins.
    """
    captured: dict[str, Any] = {}

    class _EmbedResp:
        def model_dump(self) -> dict[str, Any]:
            return {"data": [{"embedding": [0.1, 0.2]}]}

    async def fake_aembedding(**kwargs: Any) -> _EmbedResp:
        captured.update(kwargs)
        return _EmbedResp()

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", fake_aembedding)
    prefs = await _fresh_prefs()

    # No embed pref → the env default embedding model.
    await _gateway(prefs=prefs).embed(["hi"])
    assert captured["model"] == "ollama/nomic-embed-text"

    # Operator picks an embedding model in the UI → it drives embedding.
    await prefs.set_embed_default("local", "qwen3-embedding:0.6b")
    await _gateway(prefs=prefs).embed(["hi"])
    assert captured["model"] == "ollama/qwen3-embedding:0.6b"

    # An explicit model (a module's per-module override) still wins.
    await _gateway(prefs=prefs).embed(["hi"], model="bge-m3")
    assert captured["model"] == "ollama/bge-m3"


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


# ── reasoning / thinking capture (ADR-0041) ──────────────────────────────────────


async def test_chat_extracts_inline_think_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**kwargs: Any) -> _Response:
        return _Response(
            {
                "model": "ollama_chat/llama3.2",
                "choices": [{"message": {"content": "<think>ponder</think>The reply."}}],
            }
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    result = await _gateway().chat([ChatMessage(role="user", content="hi")])
    # The <think> span is lifted out of the answer into the reasoning field.
    assert result.content == "The reply."
    assert result.reasoning == "ponder"


async def test_chat_prefers_native_reasoning_field(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**kwargs: Any) -> _Response:
        return _Response(
            {
                "model": "anthropic/c",
                "choices": [{"message": {"content": "Done.", "reasoning_content": "native trace"}}],
            }
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    secrets = _FakeSecrets({"llm/anthropic": {"api_key": "k"}})
    result = await _gateway(secrets=secrets).chat(
        [ChatMessage(role="user", content="hi")], model="claude/c"
    )
    assert result.content == "Done."
    assert result.reasoning == "native trace"


def _reasoning_chunk(content: str | None = None, reasoning: str | None = None) -> Any:
    """A streaming chunk whose delta carries content and/or a reasoning_content field."""

    class _Delta:
        def __init__(self) -> None:
            self.content = content
            self.reasoning_content = reasoning
            self.tool_calls = None

    class _Choice:
        def __init__(self) -> None:
            self.delta = _Delta()

    class _Chunk:
        def __init__(self) -> None:
            self.choices = [_Choice()]

    return _Chunk()


async def test_stream_chat_surfaces_native_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_chunks() -> AsyncIterator[Any]:
        yield _reasoning_chunk(reasoning="weigh it")
        yield _reasoning_chunk(content="Answer.")

    async def fake_acompletion(**kwargs: Any) -> AsyncIterator[Any]:
        return fake_chunks()

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    events = [e async for e in _gateway().stream_chat([ChatMessage(role="user", content="hi")])]
    assert [e.reasoning for e in events if e.reasoning] == ["weigh it"]
    assert [e.delta for e in events if e.delta] == ["Answer."]
    result = next(e.result for e in events if e.result is not None)
    assert result.content == "Answer."
    assert result.reasoning == "weigh it"


async def test_stream_chat_splits_inline_think_from_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_chunks() -> AsyncIterator[Any]:
        # The <think> span is split across chunk boundaries; the answer follows.
        for piece in ["<thi", "nk>hidden</think>vis", "ible"]:
            yield _reasoning_chunk(content=piece)

    async def fake_acompletion(**kwargs: Any) -> AsyncIterator[Any]:
        return fake_chunks()

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    events = [e async for e in _gateway().stream_chat([ChatMessage(role="user", content="hi")])]
    assert "".join(e.reasoning for e in events if e.reasoning) == "hidden"
    assert "".join(e.delta for e in events if e.delta) == "visible"
    result = next(e.result for e in events if e.result is not None)
    assert result.content == "visible"
    assert result.reasoning == "hidden"


# ── LLM tuning (#114) ────────────────────────────────────────────────────────────


async def test_tuning_params_applied_to_local_call(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(
            {"model": "ollama_chat/llama3.2", "choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    await _gateway(temperature=0.2, top_p=0.8, num_ctx=8192).chat(
        [ChatMessage(role="user", content="hi")]
    )
    assert captured["temperature"] == 0.2
    assert captured["top_p"] == 0.8
    assert captured["num_ctx"] == 8192


async def test_num_ctx_is_local_only_temperature_is_universal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"model": "anthropic/c", "choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    secrets = _FakeSecrets({"llm/anthropic": {"api_key": "k"}})
    await _gateway(secrets=secrets, temperature=0.3, num_ctx=8192).chat(
        [ChatMessage(role="user", content="hi")], model="claude/claude-3-5-sonnet-latest"
    )
    assert captured["temperature"] == 0.3  # sampling knob applies to hosted too
    assert "num_ctx" not in captured  # Ollama-only runtime option, never sent to hosted


async def test_no_tuning_keys_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(
            {"model": "ollama_chat/llama3.2", "choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    await _gateway().chat([ChatMessage(role="user", content="hi")])
    assert "temperature" not in captured
    assert "top_p" not in captured
    assert "num_ctx" not in captured


# ── LLM prefs: hidden list + global default (#124) ───────────────────────────


async def test_models_marks_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    class _HttpResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"models": [{"name": "llama3.2", "size": 10}, {"name": "phi3:mini", "size": 5}]}

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
    prefs = await _fresh_prefs()
    await prefs.set_hidden("local", ["phi3:mini"])
    models = await _gateway(prefs=prefs).models()
    by_name = {m.name: m for m in models}
    assert not by_name["llama3.2"].hidden
    assert by_name["phi3:mini"].hidden


async def test_effective_default_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(
            {"model": "ollama_chat/llama3.2", "choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    prefs = await _fresh_prefs()
    # No stored default — env default ("llama3.2") must be used.
    await _gateway(prefs=prefs).chat([ChatMessage(role="user", content="hi")])
    assert captured["model"] == "ollama_chat/llama3.2"


async def test_stored_global_default_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(
            {"model": "ollama_chat/qwen2.5:7b", "choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    prefs = await _fresh_prefs()
    await prefs.set_default("local", "qwen2.5:7b")
    await _gateway(prefs=prefs).chat([ChatMessage(role="user", content="hi")])
    assert captured["model"] == "ollama_chat/qwen2.5:7b"


async def test_explicit_model_ignores_stored_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(
            {"model": "ollama_chat/mistral", "choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    prefs = await _fresh_prefs()
    await prefs.set_default("local", "qwen2.5:7b")
    # An explicit model in the request must win over the stored default.
    await _gateway(prefs=prefs).chat([ChatMessage(role="user", content="hi")], model="mistral")
    assert captured["model"] == "ollama_chat/mistral"


# ── model readiness (ADR-0027) ───────────────────────────────────────────────────


async def test_model_readiness_local_warm_matches_tagged_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = _gateway()

    async def fake_models(tenant_id: str | None = None) -> list[ModelInfo]:
        # The runtime tags the loaded model "llama3.2:latest"; the bare name must match.
        return [
            ModelInfo(name="llama3.2:latest", loaded=True),
            ModelInfo(name="qwen2.5:0.5b", loaded=False),
        ]

    monkeypatch.setattr(gw, "models", fake_models)
    assert await gw.model_readiness("llama3.2") == ("llama3.2", True)


async def test_model_readiness_local_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    gw = _gateway()

    async def fake_models(tenant_id: str | None = None) -> list[ModelInfo]:
        return [ModelInfo(name="llama3.2:latest", loaded=False)]

    monkeypatch.setattr(gw, "models", fake_models)
    assert await gw.model_readiness("llama3.2") == ("llama3.2", False)


async def test_model_readiness_hosted_is_always_ready() -> None:
    # Hosted providers need no local warm-up — warm is None (always ready), no runtime probe.
    name, warm = await _gateway().model_readiness("claude/claude-sonnet-4-6")
    assert name == "claude/claude-sonnet-4-6" and warm is None


async def test_model_readiness_paused_local_is_cold_without_probing() -> None:
    power = PowerController()
    power.pause()
    # While paused the runtime is never probed (that would wake the GPU): cold by definition.
    name, warm = await _gateway(power=power).model_readiness("llama3.2")
    assert name == "llama3.2" and warm is False


async def test_model_readiness_runtime_error_reports_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    gw = _gateway()

    async def boom(tenant_id: str | None = None) -> list[ModelInfo]:
        raise RuntimeError("ollama unreachable")

    monkeypatch.setattr(gw, "models", boom)
    assert await gw.model_readiness("llama3.2") == ("llama3.2", False)


async def test_model_readiness_defaults_to_effective_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = _gateway()  # default_model="llama3.2"

    async def fake_models(tenant_id: str | None = None) -> list[ModelInfo]:
        return [ModelInfo(name="llama3.2:latest", loaded=True)]

    monkeypatch.setattr(gw, "models", fake_models)
    assert await gw.model_readiness() == ("llama3.2", True)


# ── context window (num_ctx) pref resolution ──────────────────────────────────────


async def test_effective_context_window_falls_back_to_env() -> None:
    prefs = await _fresh_prefs()
    # No stored pref → the env default (the gateway's num_ctx constructor arg).
    assert await _gateway(prefs=prefs, num_ctx=4096).effective_context_window() == 4096
    # No pref and no env default → None (the runtime's own default applies).
    assert await _gateway(prefs=prefs).effective_context_window() is None


async def test_stored_context_window_overrides_env() -> None:
    prefs = await _fresh_prefs()
    await prefs.set_context_window("local", 16384)
    assert await _gateway(prefs=prefs, num_ctx=4096).effective_context_window() == 16384


async def test_chat_applies_context_window_pref(monkeypatch: pytest.MonkeyPatch) -> None:
    """A streamed/blocking chat turn resolves num_ctx from the pref, per turn."""
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(
            {"model": "ollama_chat/llama3.2", "choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    prefs = await _fresh_prefs()
    gw = _gateway(prefs=prefs, num_ctx=4096)

    # With no pref, the env default num_ctx is sent.
    await gw.chat([ChatMessage(role="user", content="hi")])
    assert captured["num_ctx"] == 4096

    # The operator raises the context window in the UI → the next turn uses it.
    await prefs.set_context_window("local", 16384)
    await gw.chat([ChatMessage(role="user", content="hi")])
    assert captured["num_ctx"] == 16384


async def test_context_window_pref_not_sent_to_hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"model": "anthropic/c", "choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    prefs = await _fresh_prefs()
    await prefs.set_context_window("local", 16384)
    secrets = _FakeSecrets({"llm/anthropic": {"api_key": "k"}})
    await _gateway(prefs=prefs, secrets=secrets).chat(
        [ChatMessage(role="user", content="hi")], model="claude/claude-3-5-sonnet-latest"
    )
    assert "num_ctx" not in captured  # Ollama-only runtime option, never sent to hosted


# ── per-model settings: context window + keep-alive (chat & embed) ────────────────


async def test_per_model_context_and_keep_alive_win(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(
            {"model": "ollama_chat/llama3.2", "choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    ms = await _fresh_model_settings()
    await ms.set("local", "llama3.2", ModelSettings(context_window=4096, keep_alive="1h"))
    await _gateway(model_settings=ms).chat([ChatMessage(role="user", content="hi")])
    assert captured["num_ctx"] == 4096
    assert captured["keep_alive"] == "1h"  # overrides the "5m" env default


# ── context compaction: fit the prompt to the window before the runtime truncates ──


def _long_convo(n: int) -> list[ChatMessage]:
    """A system prompt plus ``n`` chunky user turns — enough to overflow a small window."""
    body = "x" * 340
    return [ChatMessage(role="system", content="INSTRUCTIONS")] + [
        ChatMessage(role="user", content=f"turn-{i} {body}") for i in range(n)
    ]


async def test_chat_trims_history_to_fit_a_local_context_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(
            {"model": "ollama_chat/llama3.2", "choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    prefs = await _fresh_prefs()
    await prefs.set_context_window("local", 2048)  # a tight local window
    convo = _long_convo(20)
    await _gateway(prefs=prefs).chat(convo)

    sent = captured["messages"]
    assert len(sent) < len(convo)  # history was trimmed to fit
    assert sent[0]["content"] == "INSTRUCTIONS"  # the system prompt survived
    assert sent[-1]["content"].startswith("turn-19")  # the newest turn survived
    assert any("trimmed to fit the context window" in m["content"] for m in sent)  # noted


async def test_chat_does_not_trim_a_hosted_context(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"model": "anthropic/c", "choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    prefs = await _fresh_prefs()
    await prefs.set_context_window("local", 2048)
    secrets = _FakeSecrets({"llm/anthropic": {"api_key": "k"}})
    convo = _long_convo(20)
    await _gateway(prefs=prefs, secrets=secrets).chat(
        convo, model="claude/claude-3-5-sonnet-latest"
    )
    # Hosted providers have large contexts and handle overflow themselves — left untouched.
    assert len(captured["messages"]) == len(convo)


async def test_per_model_context_overrides_global_pref(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(
            {"model": "ollama_chat/llama3.2", "choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    prefs = await _fresh_prefs()
    await prefs.set_context_window("local", 16384)  # global pref
    ms = await _fresh_model_settings()
    await ms.set("local", "llama3.2", ModelSettings(context_window=4096))  # this model wins
    await _gateway(prefs=prefs, model_settings=ms).chat([ChatMessage(role="user", content="hi")])
    assert captured["num_ctx"] == 4096


async def test_per_model_settings_match_by_family_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stored under the runtime's tagged name; a request for the bare default must still match.
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(
            {"model": "ollama_chat/llama3.2", "choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    ms = await _fresh_model_settings()
    await ms.set("local", "llama3.2:latest", ModelSettings(context_window=2048))
    # Request uses the bare default model "llama3.2"; settings keyed by the tag must match.
    await _gateway(model_settings=ms).chat([ChatMessage(role="user", content="hi")])
    assert captured["num_ctx"] == 2048


async def test_per_model_falls_back_to_env_keep_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(
            {"model": "ollama_chat/llama3.2", "choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    ms = await _fresh_model_settings()  # store present but no row for this model
    await _gateway(model_settings=ms).chat([ChatMessage(role="user", content="hi")])
    assert captured["keep_alive"] == "5m"  # env default
    assert "num_ctx" not in captured  # no per-model, no global pref, no env num_ctx


async def test_embed_applies_per_model_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_aembedding(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"data": [{"embedding": [0.1, 0.2]}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", fake_aembedding)
    ms = await _fresh_model_settings()
    await ms.set("local", "nomic-embed-text", ModelSettings(context_window=512, keep_alive="10m"))
    vectors = await _gateway(model_settings=ms).embed(["hello"], model="nomic-embed-text")
    assert vectors == [[0.1, 0.2]]
    assert captured["num_ctx"] == 512
    assert captured["keep_alive"] == "10m"


async def test_embed_unset_passes_no_options(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_aembedding(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"data": [{"embedding": [0.0]}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", fake_aembedding)
    ms = await _fresh_model_settings()
    await _gateway(model_settings=ms).embed(["hello"], model="nomic-embed-text")
    assert "num_ctx" not in captured  # embeddings stay opt-in — unchanged when nothing set
    assert "keep_alive" not in captured


async def test_show_parses_quantization_and_context_length(monkeypatch: pytest.MonkeyPatch) -> None:
    class _HttpResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "details": {
                    "family": "llama",
                    "parameter_size": "8.0B",
                    "quantization_level": "Q4_K_M",
                },
                "model_info": {"general.architecture": "llama", "llama.context_length": 131072},
            }

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, path: str, json: dict[str, Any]) -> _HttpResponse:
            assert path == "/api/show"
            return _HttpResponse()

    monkeypatch.setattr("epicurus_core_app.llm.gateway.httpx.AsyncClient", _Client)
    details = await _gateway().show("llama3.2:latest")
    assert details.quantization == "Q4_K_M"
    assert details.parameter_size == "8.0B"
    assert details.context_length == 131072
    assert details.family == "llama"


# ── per-model device → Ollama num_gpu (GPU/CPU choice, #293) ──────────────────────


async def test_device_cpu_sets_num_gpu_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(
            {"model": "ollama_chat/llama3.2", "choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    ms = await _fresh_model_settings()
    await ms.set("local", "llama3.2", ModelSettings(device="cpu"))
    await _gateway(model_settings=ms).chat([ChatMessage(role="user", content="hi")])
    assert captured["num_gpu"] == 0


async def test_device_gpu_offloads_all_layers(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(
            {"model": "ollama_chat/llama3.2", "choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    ms = await _fresh_model_settings()
    await ms.set("local", "llama3.2", ModelSettings(device="gpu"))
    await _gateway(model_settings=ms).chat([ChatMessage(role="user", content="hi")])
    assert captured["num_gpu"] == 999


async def test_device_auto_omits_num_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(
            {"model": "ollama_chat/llama3.2", "choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    ms = await _fresh_model_settings()
    await ms.set("local", "llama3.2", ModelSettings(context_window=4096))  # device unset = auto
    await _gateway(model_settings=ms).chat([ChatMessage(role="user", content="hi")])
    assert "num_gpu" not in captured


async def test_embed_device_cpu_sets_num_gpu_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_aembedding(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"data": [{"embedding": [0.0]}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", fake_aembedding)
    ms = await _fresh_model_settings()
    await ms.set("local", "nomic-embed-text", ModelSettings(device="cpu"))
    await _gateway(model_settings=ms).embed(["hi"], model="nomic-embed-text")
    assert captured["num_gpu"] == 0
