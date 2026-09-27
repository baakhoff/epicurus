"""Unit tests for the LLM gateway — LiteLLM, Ollama, and OpenBao are mocked (no network)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, ClassVar, cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core import EventBus, SecretError, SecretNotFoundError, SecretStore
from epicurus_core_app.llm.errors import (
    LocalRuntimeUnavailableError,
    ModelCapabilityError,
)
from epicurus_core_app.llm.gateway import (
    _CONNECT_TIMEOUT_S,
    _UNBOUNDED_READ_S,
    NO_TOOLS_SYSTEM_NOTE,
    LlmGateway,
    _normalize_tool_calls,
    _tool_rejection_phrase,
    with_no_tools_note,
)
from epicurus_core_app.llm.model_settings import ModelSettings, ModelSettingsStore
from epicurus_core_app.llm.models import ChatMessage, ModelInfo, ModelWarmth, PowerState
from epicurus_core_app.llm.power import GatewayPausedError, PowerController
from epicurus_core_app.llm.prefs import LlmPrefsStore
from epicurus_core_app.llm.saved_models import SavedHostedModelStore, SavedModelOverride


class _FakeSecrets:
    """A stand-in for SecretStore: returns seeded secrets, else says there is none.

    ``unreachable`` makes every read fail the *other* way — the store could not be asked at
    all (an expired token, OpenBao down). The real store distinguishes the two by exception
    type, so this one must too, or no test could tell them apart either.
    """

    def __init__(
        self, data: dict[str, dict[str, Any]] | None = None, *, unreachable: bool = False
    ) -> None:
        self._data = data or {}
        self._unreachable = unreachable

    async def get(self, path: str, tenant_id: str | None = None) -> dict[str, Any]:
        if self._unreachable:
            raise SecretError(f"failed to read secret {path}: HTTP 403 — token expired")
        if path in self._data:
            return self._data[path]
        raise SecretNotFoundError(f"secret not found: {path}")


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
    timeout: float = 600.0,
    temperature: float | None = None,
    top_p: float | None = None,
    num_ctx: int | None = None,
    prefs: LlmPrefsStore | None = None,
    model_settings: ModelSettingsStore | None = None,
    saved_models: SavedHostedModelStore | None = None,
    ollama_url: str = "http://ollama:11434",
) -> LlmGateway:
    return LlmGateway(
        ollama_url=ollama_url,
        default_model="llama3.2",
        keep_alive="5m",
        power=power or PowerController(),
        # Structural stand-ins: the gateway only ever calls `get`/`set`/`delete` and `publish`.
        secrets=cast("SecretStore", secrets or _FakeSecrets()),
        default_tenant="local",
        bus=cast("EventBus", bus or _FakeBus()),
        fallbacks=fallbacks or [],
        num_retries=2,
        timeout=timeout,
        temperature=temperature,
        top_p=top_p,
        num_ctx=num_ctx,
        prefs=prefs,
        model_settings=model_settings,
        saved_models=saved_models,
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
    # Recorded, not captured — see ``_RecordingLog``. A ``capture_logs`` block that silently
    # intercepts nothing makes this particular assertion *vacuously* true, which is the worst
    # possible failure mode for a test whose whole job is to prove a key never gets logged.
    recorder = _RecordingLog()
    monkeypatch.setattr("epicurus_core_app.llm.gateway.log", recorder)
    await _gateway(secrets=secrets).chat([ChatMessage(role="user", content="hi")], model="claude/c")
    assert not any("fixture-redaction-sentinel" in str(call) for call in recorder.calls)


async def test_providers_reports_configured() -> None:
    secrets = _FakeSecrets({"llm/anthropic": {"api_key": "k"}})
    infos = {p.alias: p for p in await _gateway(secrets=secrets).providers()}
    assert infos["local"].local and infos["local"].configured
    assert infos["claude"].configured  # key seeded
    assert not infos["gpt"].configured  # no key


async def test_providers_names_the_three_key_states() -> None:
    # `configured` is one bit over three facts. It stays (existing readers depend on it);
    # `key_state` carries what it cannot say.
    secrets = _FakeSecrets({"llm/anthropic": {"api_key": "k"}})
    infos = {p.alias: p for p in await _gateway(secrets=secrets).providers()}
    assert infos["local"].key_state == "not_required"  # the local runtime holds no key
    assert infos["claude"].key_state == "present"
    assert infos["gpt"].key_state == "missing"  # OpenBao answered: nothing there
    assert all(info.key_error is None for info in infos.values())


async def test_an_unreachable_secret_store_is_not_reported_as_unconfigured() -> None:
    """#728's misdiagnosis, pinned.

    An expired app token made every hosted provider read as `configured: false`, which sends
    an operator to re-enter keys they already set instead of to the token. "We could not
    ask" is a different fact from "there is no key", and it must be said differently.
    """
    secrets = _FakeSecrets({"llm/anthropic": {"api_key": "k"}}, unreachable=True)
    infos = {p.alias: p for p in await _gateway(secrets=secrets).providers()}

    assert infos["claude"].key_state == "unavailable"
    assert infos["gpt"].key_state == "unavailable"
    # Still falsy, so nothing downstream starts routing to a provider we cannot key…
    assert not infos["claude"].configured
    # …but the payload now says *why*, and the reason names the token (the #728 hint).
    assert infos["claude"].key_error is not None
    assert "403" in infos["claude"].key_error
    # The local runtime needs no store at all, so an outage must not touch it.
    assert infos["local"].key_state == "not_required"
    assert infos["local"].configured


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


async def test_timeout_passed_to_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    # The gateway passes an explicit read timeout to litellm so a legitimate cold-load /
    # prompt-eval stall does not abort the stream at aiohttp's sock_read (#453). The read
    # component is the inter-chunk deadline; connect stays short so a down runtime fails fast.
    import httpx

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(
            {"model": "ollama_chat/llama3.2", "choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    await _gateway(timeout=123.0).chat([ChatMessage(role="user", content="hi")])
    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 123.0
    assert timeout.connect == _CONNECT_TIMEOUT_S


async def test_timeout_zero_disables_the_inter_chunk_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # LLM_TIMEOUT=0 means "no inter-chunk limit" — expressed as a very large finite read, because a
    # None read is coerced back to litellm's 600s default on the ollama_chat path (#453).
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response(
            {"model": "ollama_chat/llama3.2", "choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    await _gateway(timeout=0.0).chat([ChatMessage(role="user", content="hi")])
    assert captured["timeout"].read == _UNBOUNDED_READ_S


async def test_stream_chat_passes_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # The streaming site — the one #453 was reported on — also carries the read timeout.
    captured: dict[str, Any] = {}

    async def empty_stream() -> AsyncIterator[Any]:
        return
        yield  # pragma: no cover - makes this an (empty) async generator

    async def fake_acompletion(**kwargs: Any) -> AsyncIterator[Any]:
        captured.update(kwargs)
        return empty_stream()

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    async for _ in _gateway(timeout=321.0).stream_chat([ChatMessage(role="user", content="hi")]):
        pass
    assert captured["stream"] is True
    assert captured["timeout"].read == 321.0


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


# ── Hosted embedding models (#865) ────────────────────────────────────────────


def _embed_secrets() -> _FakeSecrets:
    return _FakeSecrets(
        {
            "llm/openrouter": {"api_key": "or-secret"},
            "llm/openai": {"api_key": "sk-secret"},
            "llm/custom": {"api_key": "cust-secret", "api_base": "http://vllm:8000/v1"},
        }
    )


async def test_hosted_embed_calls_the_provider_with_its_key_and_no_ollama_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hosted embedding id takes the provider path: LiteLLM's own id, the tenant's key from
    # OpenBao, and none of the local runtime options (num_ctx/keep_alive/num_gpu describe an
    # Ollama allocation and mean nothing to a provider). No api_base either — only the generic
    # OpenAI-compatible provider carries one.
    captured: dict[str, Any] = {}

    async def fake_aembedding(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", fake_aembedding)
    settings = await _fresh_model_settings()
    # Settings that WOULD apply to a local model of this name must not leak onto the wire.
    await settings.set("local", "text-embedding-3-small", ModelSettings(keep_alive="30m"))
    gateway = _gateway(secrets=_embed_secrets(), model_settings=settings)

    vectors = await gateway.embed(["hello"], model="gpt/text-embedding-3-small")

    assert vectors == [[0.1, 0.2, 0.3]]
    assert captured["model"] == "openai/text-embedding-3-small"
    assert captured["api_key"] == "sk-secret"
    assert captured["input"] == ["hello"]
    assert "api_base" not in captured
    assert not {"num_ctx", "keep_alive", "num_gpu"} & set(captured)


async def test_openrouter_embed_keeps_the_two_slash_model_id_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An OpenRouter model id carries a vendor segment of its own, so only the first slash is
    # the provider alias — splitting on every slash would send a truncated model name.
    captured: dict[str, Any] = {}

    async def fake_aembedding(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"data": [{"embedding": [0.0]}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", fake_aembedding)
    await _gateway(secrets=_embed_secrets()).embed(
        ["hi"], model="openrouter/openai/text-embedding-3-small"
    )
    assert captured["model"] == "openrouter/openai/text-embedding-3-small"
    assert captured["api_key"] == "or-secret"


async def test_custom_hosted_embed_reads_its_api_base_from_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_aembedding(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"data": [{"embedding": [0.0]}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", fake_aembedding)
    await _gateway(secrets=_embed_secrets()).embed(["hi"], model="custom/bge-m3")
    assert captured["model"] == "openai/bge-m3"
    assert captured["api_base"] == "http://vllm:8000/v1"


async def test_hosted_embed_without_a_stored_key_fails_like_the_chat_path() -> None:
    # Same error class the chat path raises for an unconfigured provider, so both surfaces
    # report "no key for this provider" identically.
    with pytest.raises(SecretNotFoundError):
        await _gateway().embed(["hi"], model="openrouter/openai/text-embedding-3-small")


async def test_hosted_embed_still_serves_while_the_runtime_is_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pause (ADR-0005) protects the local GPU, so it must not reach a hosted provider — the
    # same rule _is_available applies to chat. Memory recall and module indexing keep working.
    async def fake_aembedding(**kwargs: Any) -> _Response:
        return _Response({"data": [{"embedding": [0.5]}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", fake_aembedding)
    power = PowerController()
    power.pause()
    gateway = _gateway(power, secrets=_embed_secrets())

    assert await gateway.embed(["hi"], model="gpt/text-embedding-3-small") == [[0.5]]
    # mark_active is a no-op while paused, so a hosted embed can never wake the runtime.
    assert power.state is PowerState.PAUSED
    # A *local* embed on the same paused gateway is still refused.
    with pytest.raises(GatewayPausedError):
        await gateway.embed(["hi"], model="nomic-embed-text")


async def test_hosted_embed_usage_event_carries_the_tenant_and_the_real_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Constraint #1: a metered call names its tenant. A hosted embed must meter exactly like a
    # local one — under the caller's tenant, not the default, and under the model truly called.
    async def fake_aembedding(**kwargs: Any) -> _Response:
        return _Response({"data": [{"embedding": [0.0]}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", fake_aembedding)
    bus = _FakeBus()
    await _gateway(bus=bus, secrets=_embed_secrets()).embed(
        ["hi"], model="openrouter/openai/text-embedding-3-small", tenant_id="tenant-x"
    )
    subject, payload, tenant_id = bus.published[0]
    assert subject == "llm.usage"
    assert tenant_id == "tenant-x"
    assert payload["tenant"] == "tenant-x"
    assert payload["model"] == "openrouter/openai/text-embedding-3-small"


async def test_hosted_embed_is_bounded_by_the_same_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One bound for both classes rather than a per-provider rule (#466).
    async def slow_aembedding(**kwargs: Any) -> _Response:
        await asyncio.sleep(10)
        return _Response({"data": [{"embedding": [0.0]}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", slow_aembedding)
    with pytest.raises(TimeoutError):
        await _gateway(timeout=0.05, secrets=_embed_secrets()).embed(
            ["hi"], model="gpt/text-embedding-3-small"
        )


async def test_explicit_local_alias_reaches_the_runtime_as_a_bare_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `local/nomic-embed-text` used to be pasted verbatim behind `ollama/`, asking the runtime
    # for a model called "local/nomic-embed-text". Classifying through the registry strips the
    # alias the same way the chat path does.
    captured: dict[str, Any] = {}

    async def fake_aembedding(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"data": [{"embedding": [0.0]}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", fake_aembedding)
    await _gateway().embed(["hi"], model="local/nomic-embed-text")
    assert captured["model"] == "ollama/nomic-embed-text"
    assert captured["api_base"] == "http://ollama:11434"


async def test_local_embed_still_sends_its_per_model_ollama_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The local path is untouched by the hosted split: the operator's settings sheet still
    # drives the runtime options for a local embedding model.
    captured: dict[str, Any] = {}

    async def fake_aembedding(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"data": [{"embedding": [0.0]}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", fake_aembedding)
    settings = await _fresh_model_settings()
    await settings.set(
        "local", "nomic-embed-text", ModelSettings(context_window=1024, keep_alive="30m")
    )
    await _gateway(model_settings=settings).embed(["hi"], model="nomic-embed-text")
    assert captured["model"] == "ollama/nomic-embed-text"
    assert captured["num_ctx"] == 1024
    assert captured["keep_alive"] == "30m"


async def test_embed_times_out_via_asyncio_wait_for(monkeypatch: pytest.MonkeyPatch) -> None:
    # LiteLLM's ollama embeddings dispatch (llms/ollama/completion/handler.py's
    # ollama_aembeddings) never threads a timeout= kwarg through to its HTTP call — unlike the
    # chat sites, where it reaches aiohttp's sock_read — so embed() enforces the same
    # LLM_TIMEOUT-derived bound with asyncio.wait_for instead (#466). Verify it actually fires.
    async def slow_aembedding(**kwargs: Any) -> _Response:
        await asyncio.sleep(10)
        return _Response({"data": [{"embedding": [0.0]}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", slow_aembedding)
    with pytest.raises(TimeoutError):
        await _gateway(timeout=0.05).embed(["hello"])


async def test_embed_never_passes_the_inert_timeout_kwarg_to_litellm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # LiteLLM silently drops a timeout= kwarg on the ollama embeddings path (verified against
    # the pinned 1.89.3) — passing one would look protective while doing nothing. Pin the
    # omission so a future "fix" doesn't reintroduce a no-op kwarg in place of the real guard.
    captured: dict[str, Any] = {}

    async def fake_aembedding(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"data": [{"embedding": [0.0]}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", fake_aembedding)
    await _gateway().embed(["hello"])
    assert "timeout" not in captured


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


async def test_stream_chat_keeps_unindexed_tool_calls_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression for the "Extra data" crash: LiteLLM's Ollama stream parser emits each
    # *complete* tool call with no `index` (and a fresh id). Keying on `index or 0`
    # collapsed two calls into one slot and concatenated their argument strings into
    # `{…}{…}`, which loaded fine when invoking the tool but threw JSONDecodeError on the
    # next turn's replay. Each un-indexed, named fragment must land in its own slot, and
    # every stored `arguments` must stay loadable JSON.
    class _Fn:
        def __init__(self, name: str | None = None, arguments: str | None = None) -> None:
            self.name = name
            self.arguments = arguments

    class _Fragment:
        def __init__(
            self,
            index: int | None = None,
            call_id: str | None = None,
            name: str | None = None,
            arguments: str | None = None,
        ) -> None:
            self.index = index
            self.id = call_id
            self.function = _Fn(name, arguments)

    class _Delta:
        def __init__(self, tool_calls: list[_Fragment] | None = None) -> None:
            self.content = None
            self.tool_calls = tool_calls

    class _Choice:
        def __init__(self, delta: _Delta) -> None:
            self.delta = delta

    class _Chunk:
        def __init__(self, delta: _Delta) -> None:
            self.choices = [_Choice(delta)]

    async def fake_chunks() -> AsyncIterator[_Chunk]:
        # Two distinct complete calls, Ollama-style: named, full JSON args, no index.
        yield _Chunk(
            _Delta([_Fragment(call_id="a", name="create_project", arguments='{"name": "Recipes"}')])
        )
        yield _Chunk(
            _Delta([_Fragment(call_id="b", name="create_project", arguments='{"name": "Travel"}')])
        )

    async def fake_acompletion(**kwargs: Any) -> AsyncIterator[_Chunk]:
        return fake_chunks()

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    events = [
        event
        async for event in _gateway().stream_chat(
            [ChatMessage(role="user", content="make two projects")],
            tools=[{"type": "function", "function": {"name": "create_project"}}],
        )
    ]

    results = [e.result for e in events if e.result is not None]
    assert len(results) == 1
    calls = results[0].tool_calls or []
    assert len(calls) == 2  # not collapsed into one
    # Every stored arguments string loads on its own — exactly what replay does (and used
    # to crash on). The two calls stay separate, not concatenated into invalid JSON.
    decoded = [json.loads(c["function"]["arguments"]) for c in calls]
    assert {d["name"] for d in decoded} == {"Recipes", "Travel"}
    # And the assistant message the agent loop replays round-trips cleanly.
    replay = ChatMessage(role="assistant", tool_calls=calls).provider_dump()
    for tool_call in replay["tool_calls"]:
        json.loads(tool_call["function"]["arguments"])  # no JSONDecodeError


# ── streamed tool-call fragments (#654, ADR-0121) ────────────────────────────
#
# The same accumulator as above, seen from its new outward-facing side: a consumer that wants to
# watch a call being written gets one `tool_call` event per arriving fragment. What matters is
# that this is *additive* (the assembly and the final result are untouched) and that it reuses
# the slot discipline above rather than re-deriving it.


class _Fn:
    def __init__(self, name: str | None = None, arguments: Any = None) -> None:
        self.name = name
        self.arguments = arguments


class _Frag:
    def __init__(
        self,
        index: int | None = None,
        call_id: str | None = None,
        name: str | None = None,
        arguments: Any = None,
        function: _Fn | None = None,
    ) -> None:
        self.index = index
        self.id = call_id
        self.function = _Fn(name, arguments) if function is None else function


class _FragDelta:
    def __init__(self, tool_calls: list[_Frag] | None = None, content: str | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FragChunk:
    def __init__(self, delta: _FragDelta) -> None:
        self.choices = [cast(Any, type("_C", (), {"delta": delta})())]


def _stream_fragments(
    monkeypatch: pytest.MonkeyPatch, deltas: list[_FragDelta]
) -> AsyncIterator[Any]:
    async def fake_chunks() -> AsyncIterator[_FragChunk]:
        for delta in deltas:
            yield _FragChunk(delta)

    async def fake_acompletion(**kwargs: Any) -> AsyncIterator[_FragChunk]:
        return fake_chunks()

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    return _gateway().stream_chat(
        [ChatMessage(role="user", content="write it")],
        tools=[{"type": "function", "function": {"name": "write_doc"}}],
    )


async def test_stream_chat_surfaces_tool_call_fragments_as_they_arrive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _stream_fragments(
        monkeypatch,
        [
            _FragDelta(content="on it"),
            _FragDelta([_Frag(0, call_id="call_1", name="write_doc")]),
            _FragDelta([_Frag(0, arguments='{"content": "# Go')]),
            _FragDelta([_Frag(0, arguments='als"}')]),
        ],
    )
    events = [event async for event in stream]

    fragments = [e.tool_call for e in events if e.tool_call is not None]
    # One event per arriving fragment, in order, carrying only that fragment's *delta*…
    assert [f.arguments for f in fragments] == [None, '{"content": "# Go', 'als"}']
    # …while id and name are the call's as resolved so far, so a continuation still names its
    # tool and a consumer can act on identity without tracking state of its own.
    assert {f.slot for f in fragments} == {0}
    assert [f.name for f in fragments] == ["write_doc"] * 3
    assert [f.id for f in fragments] == ["call_1"] * 3
    # Additive: the content delta and the assembled result are exactly what they always were.
    assert [e.delta for e in events if e.delta] == ["on it"]
    [result] = [e.result for e in events if e.result is not None]
    call = (result.tool_calls or [])[0]
    assert call["function"]["arguments"] == '{"content": "# Goals"}'


async def test_streamed_fragments_inherit_the_unindexed_slot_discipline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #324's regression, from the new side: two un-indexed, named Ollama calls must be reported
    # under two slots. One slot would hand a consumer `{…}{…}` as though it were a single call's
    # arguments — the same "Extra data" shape, just earlier in the pipeline.
    stream = _stream_fragments(
        monkeypatch,
        [
            _FragDelta([_Frag(call_id="a", name="write_doc", arguments='{"content": "one"}')]),
            _FragDelta([_Frag(call_id="b", name="write_doc", arguments='{"content": "two"}')]),
        ],
    )
    fragments = [e.tool_call async for e in stream if e.tool_call is not None]

    assert [f.slot for f in fragments] == [0, 1]
    assert [f.arguments for f in fragments] == ['{"content": "one"}', '{"content": "two"}']
    assert [f.id for f in fragments] == ["a", "b"]


async def test_a_fragment_that_carries_nothing_new_is_not_an_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _stream_fragments(
        monkeypatch,
        [
            _FragDelta([_Frag(0, call_id="call_1", name="write_doc")]),
            _FragDelta([_Frag(0, arguments="")]),  # an empty continuation says nothing…
            _FragDelta([_Frag(0, function=None)]),  # …and neither does an empty fragment
        ],
    )
    fragments = [e.tool_call async for e in stream if e.tool_call is not None]
    assert len(fragments) == 1


async def test_whole_dict_arguments_report_no_incremental_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Some providers send the arguments as a dict in one go. The accumulator *replaces* rather
    # than appends for that flavour, so there is nothing incremental to report — the call still
    # arrives whole on the final result, it simply has no typewriter.
    stream = _stream_fragments(
        monkeypatch,
        [_FragDelta([_Frag(0, call_id="call_1", name="write_doc", arguments={"content": "hi"})])],
    )
    events = [event async for event in stream]

    [fragment] = [e.tool_call for e in events if e.tool_call is not None]
    assert fragment.arguments is None
    assert (fragment.name, fragment.id) == ("write_doc", "call_1")
    [result] = [e.result for e in events if e.result is not None]
    assert json.loads((result.tool_calls or [])[0]["function"]["arguments"]) == {"content": "hi"}


def test_normalize_tool_calls_repairs_arguments() -> None:
    # The defense-in-depth layer: whatever a provider hands us, every replayed arguments
    # value is exactly one loadable JSON string.
    repaired = _normalize_tool_calls(
        [
            {"id": "1", "type": "function", "function": {"name": "a", "arguments": {"k": 1}}},
            {
                "id": "2",
                "type": "function",
                "function": {"name": "b", "arguments": '{"k": 1}{"k": 2}'},
            },
            {"id": "3", "type": "function", "function": {"name": "c", "arguments": "not json"}},
            {"id": "4", "type": "function", "function": {"name": "d", "arguments": '{"ok": true}'}},
        ]
    )
    assert repaired is not None
    decoded = [json.loads(c["function"]["arguments"]) for c in repaired]
    assert decoded[0] == {"k": 1}  # a dict is serialized
    assert decoded[1] == {"k": 1}  # trailing duplicate object dropped
    assert decoded[2] == {}  # unparseable junk degrades to {}
    assert decoded[3] == {"ok": True}  # already-valid value preserved
    assert repaired[3]["function"]["arguments"] == '{"ok": true}'  # verbatim, no re-encoding
    assert _normalize_tool_calls(None) is None
    assert _normalize_tool_calls([]) == []


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
    assert await gw.model_readiness("llama3.2") == ModelWarmth("llama3.2", True, "local")


async def test_model_readiness_local_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    gw = _gateway()

    async def fake_models(tenant_id: str | None = None) -> list[ModelInfo]:
        return [ModelInfo(name="llama3.2:latest", loaded=False)]

    monkeypatch.setattr(gw, "models", fake_models)
    assert await gw.model_readiness("llama3.2") == ModelWarmth("llama3.2", False, "local")


async def test_model_readiness_hosted_is_always_ready() -> None:
    # Hosted providers need no local warm-up — warm is None (always ready), no runtime probe.
    warmth = await _gateway().model_readiness("claude/claude-sonnet-4-6")
    assert warmth.model == "claude/claude-sonnet-4-6"
    assert warmth.warm is None and warmth.runtime == "hosted"


async def test_model_readiness_paused_local_is_cold_without_probing() -> None:
    power = PowerController()
    power.pause()
    # While paused the runtime is never probed (that would wake the GPU): cold by definition.
    warmth = await _gateway(power=power).model_readiness("llama3.2")
    assert warmth.model == "llama3.2" and warmth.warm is False


async def test_model_readiness_runtime_error_reports_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    gw = _gateway()

    async def boom(tenant_id: str | None = None) -> list[ModelInfo]:
        raise RuntimeError("ollama unreachable")

    monkeypatch.setattr(gw, "models", boom)
    assert await gw.model_readiness("llama3.2") == ModelWarmth("llama3.2", False, "local")


async def test_model_readiness_defaults_to_effective_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = _gateway()  # default_model="llama3.2"

    async def fake_models(tenant_id: str | None = None) -> list[ModelInfo]:
        return [ModelInfo(name="llama3.2:latest", loaded=True)]

    monkeypatch.setattr(gw, "models", fake_models)
    assert await gw.model_readiness() == ModelWarmth("llama3.2", True, "local")


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


async def test_effective_kv_cache_type_reads_the_pref() -> None:
    prefs = await _fresh_prefs()
    # No stored pref → None (the runtime's f16 default applies; the suggestion assumes f16).
    assert await _gateway(prefs=prefs).effective_kv_cache_type() is None
    await prefs.set_kv_cache_type("local", "q8_0")
    assert await _gateway(prefs=prefs).effective_kv_cache_type() == "q8_0"


async def test_effective_kv_cache_type_none_without_prefs() -> None:
    # No prefs store → no stored choice → None.
    assert await _gateway().effective_kv_cache_type() is None


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
    await prefs.set_context_window("local", 2048)  # a tight *local* pref — would trim if it applied
    secrets = _FakeSecrets({"llm/anthropic": {"api_key": "k"}})
    convo = _long_convo(20)
    await _gateway(prefs=prefs, secrets=secrets).chat(
        convo, model="claude/claude-3-5-sonnet-latest"
    )
    # No per-model budget set → hosted is untouched (today's behavior). Critically, the global
    # Ollama pref (2048) — which would trim this convo on a local model — never reaches hosted.
    assert len(captured["messages"]) == len(convo)


async def test_hosted_per_model_budget_trims_history(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"model": "anthropic/c", "choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    ms = await _fresh_model_settings()
    # A per-model budget on the hosted id — the operator's spend cap / overflow guard (#570).
    await ms.set("local", "claude/claude-3-5-sonnet-latest", ModelSettings(context_window=2048))
    secrets = _FakeSecrets({"llm/anthropic": {"api_key": "k"}})
    convo = _long_convo(20)
    await _gateway(model_settings=ms, secrets=secrets).chat(
        convo, model="claude/claude-3-5-sonnet-latest"
    )

    sent = captured["messages"]
    assert len(sent) < len(convo)  # the conversation was compacted to the budget
    assert sent[0]["content"] == "INSTRUCTIONS"  # the system prompt survived
    assert sent[-1]["content"].startswith("turn-19")  # the newest turn survived
    assert any("trimmed to fit the context window" in m["content"] for m in sent)  # trim-note
    assert "num_ctx" not in captured  # the budget is never sent as an Ollama runtime option


async def test_hosted_budget_ignores_global_ollama_pref(monkeypatch: pytest.MonkeyPatch) -> None:
    # The explicit "8k global + 200k hosted" case: the global Ollama pref must not shrink a
    # generously-budgeted hosted window (#570). A long convo that an 8k budget *would* trim is
    # left whole because the per-model budget is 200k.
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"model": "anthropic/c", "choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    prefs = await _fresh_prefs()
    await prefs.set_context_window("local", 8192)  # small global Ollama pref
    ms = await _fresh_model_settings()
    await ms.set("local", "claude/claude-3-5-sonnet-latest", ModelSettings(context_window=200_000))
    secrets = _FakeSecrets({"llm/anthropic": {"api_key": "k"}})
    convo = _long_convo(100)  # ~10k tokens: over an 8k budget, far under 200k
    await _gateway(prefs=prefs, model_settings=ms, secrets=secrets).chat(
        convo, model="claude/claude-3-5-sonnet-latest"
    )
    assert len(captured["messages"]) == len(convo)  # untouched — the 8k global never applied


async def test_hosted_budget_uses_exact_id_not_local_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hosted id resolves its budget by *exact* match, never the loose family match locals use —
    # so a local `llama3.2:latest` window can't bleed into a hosted `custom/llama3.2` call (#570).
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"model": "openai/llama3.2", "choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    ms = await _fresh_model_settings()
    await ms.set("local", "llama3.2:latest", ModelSettings(context_window=2048))  # a *local* row
    secrets = _FakeSecrets({"llm/custom": {"api_key": "k", "api_base": "http://host"}})
    convo = _long_convo(20)
    await _gateway(model_settings=ms, secrets=secrets).chat(convo, model="custom/llama3.2")
    # The local family row is not the hosted budget → untouched (loose matching would have trimmed).
    assert len(captured["messages"]) == len(convo)
    assert "num_ctx" not in captured


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
                "capabilities": ["completion", "tools", "insert"],
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
    assert details.capabilities == ["completion", "tools", "insert"]


# ── tool-capability gating (a tool-less model just answers in text) ───────────────


def _show_client(capabilities: list[str]) -> type:
    """A fake httpx client whose ``/api/show`` reports the given capabilities."""

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"capabilities": capabilities}

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, path: str, json: dict[str, Any]) -> _Resp:
            return _Resp()

    return _Client


async def test_supports_tools_reads_local_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    g = "epicurus_core_app.llm.gateway.httpx.AsyncClient"
    monkeypatch.setattr(g, _show_client(["completion", "tools"]))
    assert await _gateway().supports_tools("llama3.2") is True
    monkeypatch.setattr(g, _show_client(["completion", "vision"]))
    assert await _gateway().supports_tools("llama3.2") is False
    # An empty capability list (older runtime that doesn't report them) must not restrict.
    monkeypatch.setattr(g, _show_client([]))
    assert await _gateway().supports_tools("llama3.2") is True


async def test_supports_tools_assumes_hosted_models_can() -> None:
    # A mainstream hosted model the shipped catalogue lists as tool-capable answers yes from
    # the catalogue itself (#947) — no runtime client is mocked: /api/show is never called.
    assert await _gateway().supports_tools("claude/claude-3-5-sonnet-latest") is True


# ── vision-capability gating (#633: an image attachment is only sent to a model that can
# see it) ──────────────────────────────────────────────────────────────────────────────


async def test_supports_vision_reads_local_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    g = "epicurus_core_app.llm.gateway.httpx.AsyncClient"
    monkeypatch.setattr(g, _show_client(["completion", "vision"]))
    assert await _gateway().supports_vision("llama3.2") is True
    monkeypatch.setattr(g, _show_client(["completion", "tools"]))
    assert await _gateway().supports_vision("llama3.2") is False
    # Unlike supports_tools, an empty/unreported capability list defaults to False here — the
    # failure mode (an image silently ignored, or a provider 400) is worse than being over-strict.
    monkeypatch.setattr(g, _show_client([]))
    assert await _gateway().supports_vision("llama3.2") is False


async def test_supports_vision_asks_litellm_for_hosted_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_supports_vision(model: str) -> bool:
        seen["model"] = model
        return True

    monkeypatch.setattr(
        "epicurus_core_app.llm.gateway.litellm.supports_vision", fake_supports_vision
    )
    assert await _gateway().supports_vision("claude/claude-3-7-sonnet-20250219") is True
    assert seen["model"] == "anthropic/claude-3-7-sonnet-20250219"  # resolved via the registry


async def test_supports_vision_defaults_false_when_litellm_has_no_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(model: str) -> bool:
        raise Exception("This model isn't mapped yet.")  # litellm's own bare-Exception shape

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.supports_vision", boom)
    assert await _gateway().supports_vision("custom/some-unlisted-model") is False


async def test_show_hosted_reports_capabilities_and_context_length_from_litellm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get_model_info(model: str) -> dict[str, Any]:
        assert model == "anthropic/claude-3-7-sonnet-20250219"
        return {
            "max_input_tokens": 200000,
            "supports_vision": True,
            "supports_function_calling": True,
            "mode": "chat",
        }

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.get_model_info", fake_get_model_info)
    details = await _gateway().show("claude/claude-3-7-sonnet-20250219")
    assert details.context_length == 200000
    assert details.capabilities == ["tools", "vision"]
    assert details.role == "chat"
    assert details.supports_tools is True
    assert details.in_catalogue is True
    # No /api/show call was made for a hosted model — no runtime client is mocked here.


async def test_show_hosted_omits_vision_and_context_when_litellm_reports_neither(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model the catalogue *does* list, and lists as tool-less, is reported tool-less (#947).

    The "assume yes" default is for an id the map has never heard of, not for one it answers
    about — see the next test. Either way the operator sees the missing badge and can override.
    """

    def fake_get_model_info(model: str) -> dict[str, Any]:
        return {"supports_vision": False}

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.get_model_info", fake_get_model_info)
    details = await _gateway().show("gpt/some-text-only-model")
    assert details.context_length is None
    assert details.capabilities == []
    assert details.supports_tools is False


async def test_show_hosted_degrades_to_tools_only_when_model_is_unmapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(model: str) -> dict[str, Any]:
        raise Exception("This model isn't mapped yet.")

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.get_model_info", boom)
    details = await _gateway().show("custom/some-unlisted-model")
    assert details.context_length is None  # never a fake default
    assert details.capabilities == ["tools"]  # unknown means yes, for tools only (#947)
    assert details.supports_tools is True
    assert details.in_catalogue is False  # and the miss is now *reported*, not only logged (#879)
    assert details.role == "unknown"


async def test_models_with_capabilities_enriches_each(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        def __init__(self, data: dict[str, Any]) -> None:
            self._data = data

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._data

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, path: str) -> _Resp:
            if path == "/api/tags":
                return _Resp({"models": [{"name": "a:1", "size": 1}, {"name": "b:1", "size": 2}]})
            return _Resp({"models": []})  # /api/ps

        async def post(self, path: str, json: dict[str, Any]) -> _Resp:
            caps = {"a:1": ["tools"], "b:1": ["vision"]}.get(json["model"], [])
            # "a:1" reports a trained context length (#618); "b:1" has no model_info at all
            # (an older runtime) — context_length must stay None, never a fake default.
            info = (
                {"general.architecture": "llama", "llama.context_length": 8192}
                if json["model"] == "a:1"
                else {}
            )
            return _Resp({"capabilities": caps, "model_info": info})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.httpx.AsyncClient", _Client)
    enriched = {m.name: m for m in await _gateway().models(with_capabilities=True)}
    assert enriched["a:1"].capabilities == ["tools"]
    assert enriched["a:1"].context_length == 8192
    assert enriched["b:1"].capabilities == ["vision"]
    assert enriched["b:1"].context_length is None
    # Without the flag there are no per-model /api/show calls; capabilities/context stay empty.
    plain = await _gateway().models()
    assert all(m.capabilities == [] and m.context_length is None for m in plain)


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


# ── per-saved-model capability overrides (#711) ────────────────────────────────
#
# LiteLLM's static cost map omits ids entirely (xai/grok-latest) and mislabels others, which made
# supports_vision() resolve False and the image gate (#633) refuse a genuinely vision-capable
# model. The operator's override is consulted *before* the map, everywhere the map is consulted.


async def _saved_store(overrides: dict[str, SavedModelOverride]) -> SavedHostedModelStore:
    """A real store seeded with saved models + their overrides (file-free in-memory SQLite)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = SavedHostedModelStore(engine)
    await store.init()
    for model, override in overrides.items():
        await store.add("local", model)
        await store.set_override("local", model, override)
    return store


async def test_vision_override_on_beats_an_unmapped_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline case: an id LiteLLM has never heard of still accepts image turns."""
    called = False

    def boom(model: str) -> bool:
        nonlocal called
        called = True
        raise Exception("This model isn't mapped yet.")

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.supports_vision", boom)
    store = await _saved_store({"grok/grok-latest": SavedModelOverride(vision="on")})
    assert await _gateway(saved_models=store).supports_vision("grok/grok-latest") is True
    # The map isn't even consulted once the operator has answered the question.
    assert called is False


async def test_vision_override_off_beats_a_map_that_says_yes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.supports_vision", lambda model: True)
    store = await _saved_store({"gpt/gpt-4o": SavedModelOverride(vision="off")})
    assert await _gateway(saved_models=store).supports_vision("gpt/gpt-4o") is False


async def test_vision_auto_falls_back_to_the_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.supports_vision", lambda model: True)
    # A saved model whose override is explicitly "auto" (and a context length set) still asks
    # the map about vision — the two fields are independent.
    store = await _saved_store({"gpt/gpt-4o": SavedModelOverride(context_length=99)})
    assert await _gateway(saved_models=store).supports_vision("gpt/gpt-4o") is True


async def test_vision_is_unchanged_for_a_model_with_no_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "epicurus_core_app.llm.gateway.litellm.supports_vision", lambda model: False
    )
    store = await _saved_store({"grok/grok-latest": SavedModelOverride(vision="on")})
    # The override is per-model: a *different* saved id keeps the map's answer.
    assert await _gateway(saved_models=store).supports_vision("gpt/gpt-4o") is False


async def test_no_store_wired_is_todays_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.supports_vision", lambda model: True)
    assert await _gateway().supports_vision("gpt/gpt-4o") is True


async def test_a_broken_store_degrades_to_the_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """A capability *hint* must never be able to break a capability *check*."""

    class _BrokenStore:
        async def get_override(self, tenant: str, model: str) -> SavedModelOverride:
            raise RuntimeError("database is gone")

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.supports_vision", lambda model: True)
    gateway = _gateway(saved_models=cast("Any", _BrokenStore()))
    assert await gateway.supports_vision("gpt/gpt-4o") is True


async def test_show_applies_the_override_over_the_map(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get_model_info(model: str) -> dict[str, Any]:
        return {
            "max_input_tokens": 8_000,
            "supports_vision": False,
            "supports_function_calling": True,
        }

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.get_model_info", fake_get_model_info)
    store = await _saved_store(
        {"grok/grok-latest": SavedModelOverride(vision="on", context_length=256_000)}
    )
    details = await _gateway(saved_models=store).show("grok/grok-latest")
    assert details.capabilities == ["tools", "vision"]
    assert details.context_length == 256_000


async def test_show_applies_the_override_even_when_the_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An id absent from the map is exactly what the override exists for — so the failure path
    must not be the one path that skips it."""

    def boom(model: str) -> dict[str, Any]:
        raise Exception("This model isn't mapped yet.")

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.get_model_info", boom)
    store = await _saved_store(
        {"grok/grok-latest": SavedModelOverride(vision="on", context_length=256_000)}
    )
    details = await _gateway(saved_models=store).show("grok/grok-latest")
    assert details.capabilities == ["tools", "vision"]
    assert details.context_length == 256_000


async def test_show_without_an_override_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get_model_info(model: str) -> dict[str, Any]:
        return {
            "max_input_tokens": 200_000,
            "supports_vision": True,
            "supports_function_calling": True,
        }

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.get_model_info", fake_get_model_info)
    store = await _saved_store({"grok/grok-latest": SavedModelOverride(vision="off")})
    details = await _gateway(saved_models=store).show("claude/claude-3-7-sonnet-20250219")
    assert details.capabilities == ["tools", "vision"]
    assert details.context_length == 200_000


class _RecordingLog:
    """Records level + fields per call.

    Asserted against directly rather than through ``structlog.testing.capture_logs``: the app
    configures structlog with ``cache_logger_on_first_use=True``, so whichever test boots it
    first freezes the gateway module's bound logger and a later ``capture_logs()`` silently
    intercepts nothing — the assertion would pass alone and fail in a full run.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.calls.append(("warning", event, fields))

    def debug(self, event: str, **fields: Any) -> None:
        self.calls.append(("debug", event, fields))

    def info(self, event: str, **fields: Any) -> None:
        self.calls.append(("info", event, fields))

    def error(self, event: str, **fields: Any) -> None:
        self.calls.append(("error", event, fields))

    def levels(self) -> list[str]:
        return [level for level, _, _ in self.calls]

    def find(self, prefix: str) -> tuple[str, str, dict[str, Any]]:
        """The first call whose event starts with ``prefix`` — asserts that there is one."""
        found = next((call for call in self.calls if call[1].startswith(prefix)), None)
        assert found is not None, f"no log line starting {prefix!r}; recorded {self.calls}"
        return found


async def test_an_unmapped_model_warns_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator-saved alias outside the map is expected — worth one warning, not a stream."""

    def boom(model: str) -> dict[str, Any]:
        raise Exception("This model isn't mapped yet.")

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.get_model_info", boom)
    recorder = _RecordingLog()
    monkeypatch.setattr("epicurus_core_app.llm.gateway.log", recorder)
    gateway = _gateway()
    for _ in range(4):
        await gateway.show("custom/some-unlisted-model")
    await gateway.show("custom/another-unlisted-model")

    # First sighting of each distinct id warns; the repeats drop to debug.
    assert recorder.levels() == ["warning", "debug", "debug", "debug", "warning"]
    warned = [f["model"] for level, _, f in recorder.calls if level == "warning"]
    assert warned == ["openai/some-unlisted-model", "openai/another-unlisted-model"]


async def test_the_unmapped_model_memo_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """``model`` reaches the gateway from a route query param, so the memo can't grow forever."""

    def boom(model: str) -> dict[str, Any]:
        raise Exception("This model isn't mapped yet.")

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.get_model_info", boom)
    monkeypatch.setattr("epicurus_core_app.llm.gateway.log", _RecordingLog())
    gateway = _gateway()
    for i in range(600):
        await gateway.show(f"custom/unlisted-{i}")
    assert len(gateway._unmapped_models) <= 512


# ── Model role: chat is not embedding (#944, ADR-0140) ────────────────────────


def _hosted_map(monkeypatch: pytest.MonkeyPatch, entries: dict[str, dict[str, Any]]) -> None:
    """Stand in for LiteLLM's static cost map, raising its bare Exception for a miss."""

    def fake_get_model_info(model: str) -> dict[str, Any]:
        if model not in entries:
            raise Exception("This model isn't mapped yet.")
        return entries[model]

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.get_model_info", fake_get_model_info)


async def test_model_role_reads_the_catalogue_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _hosted_map(
        monkeypatch,
        {
            "openrouter/qwen/qwen3-embedding-8b": {"mode": "embedding"},
            "anthropic/claude-sonnet-4-6": {"mode": "chat"},
        },
    )
    gw = _gateway()
    assert await gw.model_role("openrouter/qwen/qwen3-embedding-8b") == "embedding"
    assert await gw.model_role("claude/claude-sonnet-4-6") == "chat"
    # An id the map has never heard of stays "unknown" — and is therefore refused nothing.
    assert await gw.model_role("custom/never-listed") == "unknown"


async def test_role_override_beats_the_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    _hosted_map(monkeypatch, {"xai/grok-latest": {"mode": "embedding"}})
    store = await _saved_store({"grok/grok-latest": SavedModelOverride(role="chat")})
    assert await _gateway(saved_models=store).model_role("grok/grok-latest") == "chat"


async def test_chat_refuses_an_embedding_model_before_any_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#944's incident: the chat default was an embedding id and only OpenRouter said so."""
    called = False

    async def fake_acompletion(**kwargs: Any) -> _Response:
        nonlocal called
        called = True
        return _Response({"choices": [{"message": {"content": "hi"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    _hosted_map(monkeypatch, {"openrouter/qwen/qwen3-embedding-8b": {"mode": "embedding"}})
    with pytest.raises(ModelCapabilityError) as caught:
        await _gateway().chat(
            [ChatMessage(role="user", content="hi")],
            model="openrouter/qwen/qwen3-embedding-8b",
        )
    assert called is False
    assert caught.value.capability == "chat"
    assert "embedding model" in str(caught.value)
    assert "Models page" in caught.value.hint


async def test_chat_refusal_is_not_papered_over_by_the_fallback_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong default is a misconfiguration to surface, not a provider fault to route around."""

    async def fake_acompletion(**kwargs: Any) -> _Response:
        raise AssertionError("no provider call should happen")

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    _hosted_map(monkeypatch, {"openrouter/an-embedder": {"mode": "embedding"}})
    gw = _gateway(fallbacks=["claude/claude-sonnet-4-6"])
    with pytest.raises(ModelCapabilityError):
        await gw.chat([ChatMessage(role="user", content="hi")], model="openrouter/an-embedder")


async def test_stream_chat_refuses_an_embedding_model(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**kwargs: Any) -> AsyncIterator[Any]:
        raise AssertionError("no provider call should happen")

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    _hosted_map(monkeypatch, {"openrouter/an-embedder": {"mode": "embedding"}})
    with pytest.raises(ModelCapabilityError):
        async for _ in _gateway().stream_chat(
            [ChatMessage(role="user", content="hi")], model="openrouter/an-embedder"
        ):
            pass


async def test_stream_refuses_an_embedding_model(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**kwargs: Any) -> AsyncIterator[Any]:
        raise AssertionError("no provider call should happen")

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    _hosted_map(monkeypatch, {"openrouter/an-embedder": {"mode": "embedding"}})
    with pytest.raises(ModelCapabilityError):
        async for _ in _gateway().stream(
            [ChatMessage(role="user", content="hi")], model="openrouter/an-embedder"
        ):
            pass


async def test_embed_refuses_a_chat_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mirror rule: the Embedding-model card's old warning is now enforced (#944)."""

    async def fake_aembedding(**kwargs: Any) -> Any:
        raise AssertionError("no provider call should happen")

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", fake_aembedding)
    _hosted_map(monkeypatch, {"anthropic/claude-sonnet-4-6": {"mode": "chat"}})
    with pytest.raises(ModelCapabilityError) as caught:
        await _gateway().embed(["hello"], model="claude/claude-sonnet-4-6")
    assert caught.value.capability == "embedding"


async def test_an_unknown_role_is_refused_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A catalogue miss must never lock the operator out of a model that works."""
    seen: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        seen.update(kwargs)
        return _Response({"choices": [{"message": {"content": "hi"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    _hosted_map(monkeypatch, {})
    result = await _gateway(secrets=_FakeSecrets({"llm/openrouter": {"api_key": "k"}})).chat(
        [ChatMessage(role="user", content="hi")], model="openrouter/brand/new-model"
    )
    assert result.content == "hi"
    assert seen["model"] == "openrouter/brand/new-model"


async def test_embed_still_refuses_a_local_model_while_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pause rule moved into the shared gate; it must behave exactly as it did (ADR-0005)."""

    async def fake_aembedding(**kwargs: Any) -> Any:
        raise AssertionError("no provider call should happen")

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", fake_aembedding)
    power = PowerController()
    power.pause()
    with pytest.raises(GatewayPausedError):
        await _gateway(power=power).embed(["hello"], model="nomic-embed-text")


async def test_a_paused_local_chat_default_still_falls_back_to_hosted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chat paths keep expressing the pause as a *filter*, not as a refusal (#944 guard)."""

    async def fake_acompletion(**kwargs: Any) -> _Response:
        assert kwargs["model"] == "anthropic/claude-sonnet-4-6"
        return _Response({"choices": [{"message": {"content": "from the cloud"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    _hosted_map(monkeypatch, {})
    power = PowerController()
    power.pause()
    gw = _gateway(
        power=power,
        secrets=_FakeSecrets({"llm/anthropic": {"api_key": "k"}}),
        fallbacks=["claude/claude-sonnet-4-6"],
    )
    result = await gw.chat([ChatMessage(role="user", content="hi")])
    assert result.content == "from the cloud"


# ── Tool support: learned from the provider, not guessed (#947, ADR-0140) ─────

# The owner's real payload, unescaped from #947's report. The account identifier is a
# placeholder; everything else is verbatim, including the triple nesting an aggregator
# produces when it wraps its upstream's wrapped error.
TOOL_REJECTION_PAYLOAD = (
    'litellm.BadRequestError: OpenrouterException - {"error":{"message":"Provider returned '
    'error","code":400,"metadata":{"raw":"{\\"error\\": {\\"message\\": '
    '\\"{\\\\\\"error\\\\\\":{\\\\\\"message\\\\\\":\\\\\\"\\\\\\"auto\\\\\\" tool choice '
    "requires --enable-auto-tool-choice and --tool-call-parser to be "
    'set\\\\\\",\\\\\\"type\\\\\\":\\\\\\"BadRequestError\\\\\\",\\\\\\"param\\\\\\":null,'
    '\\\\\\"code\\\\\\":400}}\\", \\"type\\": \\"invalid_request_error\\", \\"param\\": null, '
    '\\"code\\": 400}}","provider_name":"NextBit","is_byok":false,'
    '"provider_error_code":"400"}},"user_id":"user_2abcDEF"}\nLiteLLM Retried: 2 times'
)

# #944's sibling failure — also a 400, also from OpenRouter, and emphatically *not* about tools.
EMBEDDING_REJECTION_PAYLOAD = (
    'litellm.BadRequestError: OpenrouterException - {"error":{"message":'
    '"qwen/qwen3-embedding-8b is an embedding model and cannot be used with the '
    'chat/completions endpoint. Use the /embeddings endpoint instead.","code":400}}'
)


class _BadRequest(Exception):
    """A stand-in for ``litellm.BadRequestError``: the text plus the status code."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_the_matcher_recognises_the_real_payload() -> None:
    assert _tool_rejection_phrase(_BadRequest(TOOL_REJECTION_PAYLOAD)) == "tool choice requires"


@pytest.mark.parametrize(
    "text",
    [
        "Error: tools is not supported by this model",
        "this deployment does not support tools",
        "function calling is not supported for the selected model",
        "tool use is not supported here",
        "unknown parameter: tool_choice",
    ],
)
def test_the_matcher_recognises_the_other_known_shapes(text: str) -> None:
    assert _tool_rejection_phrase(_BadRequest(text)) is not None


def test_the_matcher_ignores_a_non_tool_400() -> None:
    """#944's rejection is the one that must *not* be learned as "no tool support"."""
    assert _tool_rejection_phrase(_BadRequest(EMBEDDING_REJECTION_PAYLOAD)) is None


def test_the_matcher_ignores_a_server_error_that_mentions_tools() -> None:
    """An outage is not a capability. Learning from one would disable every module."""
    assert _tool_rejection_phrase(_BadRequest("tool_choice handler crashed", 503)) is None


async def test_a_tool_rejection_is_retried_once_without_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    async def fake_acompletion(**kwargs: Any) -> _Response:
        calls.append(kwargs.get("tools"))
        if kwargs.get("tools"):
            raise _BadRequest(TOOL_REJECTION_PAYLOAD)
        return _Response({"choices": [{"message": {"content": "hi there"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    _hosted_map(monkeypatch, {})
    store = await _saved_store({"openrouter/some/model": SavedModelOverride()})
    gw = _gateway(secrets=_FakeSecrets({"llm/openrouter": {"api_key": "k"}}), saved_models=store)
    result = await gw.chat(
        [ChatMessage(role="user", content="hi")],
        model="openrouter/some/model",
        tools=[{"type": "function", "function": {"name": "now"}}],
    )
    assert result.content == "hi there"
    # Exactly two calls: the one that carried tools, then the one that did not.
    assert len(calls) == 2
    assert calls[1] is None
    # And the answer is remembered, for this tenant, in its own column.
    override = await store.get_override("local", "openrouter/some/model")
    assert override.tools_learned == "off"
    assert override.tools == "auto"  # the operator has still said nothing


async def test_a_second_tool_rejection_is_a_real_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry carries no tools, so a rejection of *that* is not about tools — it propagates."""
    calls = 0

    async def fake_acompletion(**kwargs: Any) -> _Response:
        nonlocal calls
        calls += 1
        raise _BadRequest(TOOL_REJECTION_PAYLOAD)

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    _hosted_map(monkeypatch, {})
    store = await _saved_store({"openrouter/some/model": SavedModelOverride()})
    gw = _gateway(secrets=_FakeSecrets({"llm/openrouter": {"api_key": "k"}}), saved_models=store)
    with pytest.raises(_BadRequest):
        await gw.chat(
            [ChatMessage(role="user", content="hi")],
            model="openrouter/some/model",
            tools=[{"type": "function", "function": {"name": "now"}}],
        )
    assert calls == 2  # the tool call, then the tool-less retry — never a third


async def test_the_learned_answer_is_written_for_the_calling_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constraint #1: a provider's deployment is a per-tenant fact, because the key is."""

    async def fake_acompletion(**kwargs: Any) -> _Response:
        if kwargs.get("tools"):
            raise _BadRequest(TOOL_REJECTION_PAYLOAD)
        return _Response({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    _hosted_map(monkeypatch, {})
    store = await _saved_store({})
    await store.add("tenant-b", "openrouter/some/model")
    gw = _gateway(secrets=_FakeSecrets({"llm/openrouter": {"api_key": "k"}}), saved_models=store)
    await gw.chat(
        [ChatMessage(role="user", content="hi")],
        model="openrouter/some/model",
        tenant_id="tenant-b",
        tools=[{"type": "function", "function": {"name": "now"}}],
    )
    assert (await store.get_override("tenant-b", "openrouter/some/model")).tools_learned == "off"
    assert (await store.get_override("local", "openrouter/some/model")).tools_learned is None


async def test_the_learn_warning_names_the_cause_and_no_account_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_acompletion(**kwargs: Any) -> _Response:
        if kwargs.get("tools"):
            raise _BadRequest(TOOL_REJECTION_PAYLOAD)
        return _Response({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    _hosted_map(monkeypatch, {})
    store = await _saved_store({"openrouter/some/model": SavedModelOverride()})
    gw = _gateway(secrets=_FakeSecrets({"llm/openrouter": {"api_key": "k"}}), saved_models=store)
    # Recorded directly rather than through ``capture_logs`` — see ``_RecordingLog``: once any
    # test in the run has booted the app, the gateway's module logger is frozen and a capture
    # here intercepts nothing, so this assertion passed alone and failed in a full run.
    recorder = _RecordingLog()
    monkeypatch.setattr("epicurus_core_app.llm.gateway.log", recorder)
    await gw.chat(
        [ChatMessage(role="user", content="hi")],
        model="openrouter/some/model",
        tools=[{"type": "function", "function": {"name": "now"}}],
    )
    level, _, fields = recorder.find("model rejected the tool list")
    assert level == "warning"
    assert fields["provider"] == "openrouter"
    assert fields["upstream"] == "NextBit"  # the aggregator's readable provider name
    assert fields["matched"] == "tool choice requires"
    # The raw body — and the account identifier riding in it — never reaches the log line.
    assert "user_2abcDEF" not in json.dumps(fields, default=str)


async def test_a_learned_no_disables_tools_on_the_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hosted_map(monkeypatch, {"openrouter/some/model": {"supports_function_calling": True}})
    store = await _saved_store({"openrouter/some/model": SavedModelOverride()})
    gw = _gateway(saved_models=store)
    assert await gw.supports_tools("openrouter/some/model") is True
    await store.learn_tools_unsupported("local", "openrouter/some/model")
    assert await gw.supports_tools("openrouter/some/model") is False


async def test_the_operator_override_beats_the_learned_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`on` is authoritative — a mis-learn is one click to undo, and the click clears it."""
    _hosted_map(monkeypatch, {})
    store = await _saved_store({})
    await store.add("local", "openrouter/some/model")
    await store.learn_tools_unsupported("local", "openrouter/some/model")
    gw = _gateway(saved_models=store)
    assert await gw.supports_tools("openrouter/some/model") is False
    await store.set_override("local", "openrouter/some/model", SavedModelOverride(tools="on"))
    assert await gw.supports_tools("openrouter/some/model") is True
    assert (await store.get_override("local", "openrouter/some/model")).tools_learned is None


async def test_an_embedding_model_reports_no_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    _hosted_map(
        monkeypatch,
        {"openrouter/qwen/qwen3-embedding-8b": {"mode": "embedding"}},
    )
    details = await _gateway().show("openrouter/qwen/qwen3-embedding-8b")
    assert details.capabilities == ["embedding"]
    assert details.supports_tools is False


def test_the_no_tools_note_lands_inside_the_protected_system_prefix() -> None:
    convo = [
        ChatMessage(role="system", content="base prompt"),
        ChatMessage(role="system", content="recalled facts"),
        ChatMessage(role="user", content="hi"),
    ]
    noted = with_no_tools_note(convo)
    assert [m.role for m in noted] == ["system", "system", "system", "user"]
    assert noted[2].content == NO_TOOLS_SYSTEM_NOTE
    assert convo[0].content == "base prompt"  # the input list is not mutated


# ── no local runtime at all (#962, ADR-0144) ─────────────────────────────────────
#
# Three states, not two: *absent* (OLLAMA_URL blank — a deliberate hosted-only deployment),
# *unreachable* (one is configured and does not answer) and *ok*. Every test below pins a
# place that used to collapse them — into a 500, into a forever-"warming" readiness, into a
# connection error where a sentence belonged.


class _StubOllama:
    """An httpx.AsyncClient stand-in that records every request path it is asked for.

    Constructed with ``boom=True`` it raises the transport error a refused connection gives,
    which is how "unreachable" is expressed; the recorded paths are how "absent" is proven —
    a deployment with no runtime must make **no call at all**, not a call that fails quietly.
    """

    paths: ClassVar[list[str]] = []

    def __init__(self, *args: Any, boom: bool = False, **kwargs: Any) -> None:
        self._boom = boom

    async def __aenter__(self) -> _StubOllama:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def _answer(self, path: str) -> Any:
        type(self).paths.append(path)
        if self._boom:
            raise httpx.ConnectError("connection refused")

        class _Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return {"models": [{"name": "llama3.2:latest", "size": 1}]}

        return _Resp()

    async def get(self, path: str) -> Any:
        return self._answer(path)

    async def post(self, path: str, **kwargs: Any) -> Any:
        return self._answer(path)

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._answer(path)


def _stub_runtime(monkeypatch: pytest.MonkeyPatch, *, boom: bool = False) -> list[str]:
    """Point the gateway's httpx client at :class:`_StubOllama`; return the recorded paths."""
    _StubOllama.paths = []

    def factory(*args: Any, **kwargs: Any) -> _StubOllama:
        return _StubOllama(boom=boom)

    monkeypatch.setattr("epicurus_core_app.llm.gateway.httpx.AsyncClient", factory)
    return _StubOllama.paths


def test_a_blank_url_is_the_absent_state() -> None:
    assert _gateway(ollama_url="").local_runtime_enabled is False
    assert _gateway(ollama_url="   ").local_runtime_enabled is False
    assert _gateway().local_runtime_enabled is True


async def test_local_runtime_state_reports_absent_without_touching_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _stub_runtime(monkeypatch)
    assert await _gateway(ollama_url="").local_runtime_state() == "absent"
    assert paths == []  # no URL, no call — absence is a configuration fact, not a probe result


async def test_local_runtime_state_reports_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_runtime(monkeypatch, boom=True)
    assert await _gateway().local_runtime_state() == "unreachable"


async def test_local_runtime_state_reports_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _stub_runtime(monkeypatch)
    assert await _gateway().local_runtime_state() == "ok"
    assert paths == ["/api/tags"]


async def test_models_is_empty_and_quiet_when_the_runtime_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Half of the regression test for the 500 the Models page collected every 10 seconds."""
    paths = _stub_runtime(monkeypatch)
    assert await _gateway(ollama_url="").models() == []
    assert paths == []


async def test_models_is_empty_when_the_runtime_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: a configured runtime that refuses the connection must not raise."""
    _stub_runtime(monkeypatch, boom=True)
    assert await _gateway().models() == []


async def test_show_reports_nothing_without_a_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _stub_runtime(monkeypatch)
    details = await _gateway(ollama_url="").show("llama3.2")
    assert details.role == "unknown" and details.capabilities == []
    assert paths == []


async def test_pull_and_delete_refuse_when_there_is_no_runtime() -> None:
    gw = _gateway(ollama_url="")
    with pytest.raises(LocalRuntimeUnavailableError) as pulled:
        await gw.pull("llama3.2")
    with pytest.raises(LocalRuntimeUnavailableError) as deleted:
        await gw.delete_model("llama3.2")
    for excinfo in (pulled, deleted):
        assert excinfo.value.state == "absent"
        assert "no local LLM runtime" in str(excinfo.value)


async def test_pull_stream_refuses_on_the_first_step() -> None:
    """The refusal must be reachable before any progress event — the route turns it into 409."""
    stream = _gateway(ollama_url="").pull_stream("llama3.2")
    with pytest.raises(LocalRuntimeUnavailableError):
        await anext(stream)


async def test_unload_is_a_quiet_no_op_without_a_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`unload` is on the power-pause path, which must keep working on a hosted-only box."""
    paths = _stub_runtime(monkeypatch)
    await _gateway(ollama_url="").unload()
    await _gateway(ollama_url="").unload("llama3.2")
    assert paths == []


async def test_model_readiness_reports_n_a_when_there_is_no_runtime() -> None:
    warmth = await _gateway(ollama_url="").model_readiness("llama3.2")
    assert warmth == ModelWarmth("llama3.2", None, "absent")


async def test_a_hosted_model_still_embeds_while_the_runtime_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner's directive, pinned: with Ollama off, embeddings go straight to the provider."""
    captured: dict[str, Any] = {}

    class _EmbedResp:
        def model_dump(self) -> dict[str, Any]:
            return {"data": [{"embedding": [0.1, 0.2]}]}

    async def fake_aembedding(**kwargs: Any) -> _EmbedResp:
        captured.update(kwargs)
        return _EmbedResp()

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.aembedding", fake_aembedding)
    _hosted_map(monkeypatch, {"openai/text-embedding-3-small": {"mode": "embedding"}})
    gw = _gateway(ollama_url="", secrets=_FakeSecrets({"llm/openai": {"api_key": "k"}}))
    vectors = await gw.embed(["hello"], model="gpt/text-embedding-3-small")

    assert vectors == [[0.1, 0.2]]
    assert captured["model"] == "openai/text-embedding-3-small"
    assert captured["api_key"] == "k"
    assert "api_base" not in captured  # no Ollama endpoint anywhere near a hosted embedding


async def test_a_local_embedding_model_refuses_with_a_reason_when_absent() -> None:
    """Not a connection error to a blank URL — a capability refusal naming the fix."""
    with pytest.raises(ModelCapabilityError) as excinfo:
        await _gateway(ollama_url="").embed(["hi"], model="nomic-embed-text")
    assert excinfo.value.capability == "embedding"
    assert "no local runtime" in excinfo.value.hint.lower()
    assert "hosted" in excinfo.value.hint.lower()


async def test_the_default_embedding_model_refuses_when_it_is_local_and_absent() -> None:
    """The out-of-the-box hosted-only install: a bare env default with nothing to run it."""
    with pytest.raises(ModelCapabilityError):
        await _gateway(ollama_url="").embed(["hi"])


async def test_a_hosted_model_still_chats_while_the_runtime_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        captured.update(kwargs)
        return _Response({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    _hosted_map(monkeypatch, {"anthropic/c": {"mode": "chat"}})
    gw = _gateway(ollama_url="", secrets=_FakeSecrets({"llm/anthropic": {"api_key": "k"}}))
    result = await gw.chat([ChatMessage(role="user", content="hi")], model="claude/c")

    assert result.content == "ok"
    assert captured["model"] == "anthropic/c"


async def test_a_local_chat_model_refuses_with_the_hint_when_absent() -> None:
    with pytest.raises(ModelCapabilityError) as excinfo:
        await _gateway(ollama_url="").chat([ChatMessage(role="user", content="hi")])
    assert excinfo.value.capability == "chat"
    assert "no local runtime" in excinfo.value.hint.lower()


async def test_a_local_fallback_is_skipped_when_there_is_no_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hosted primary must never fall back into a runtime that is not there."""
    calls: list[str] = []

    async def fake_acompletion(**kwargs: Any) -> _Response:
        calls.append(str(kwargs["model"]))
        if kwargs["model"] == "anthropic/c":
            raise RuntimeError("provider down")
        return _Response({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("epicurus_core_app.llm.gateway.litellm.acompletion", fake_acompletion)
    _hosted_map(monkeypatch, {"anthropic/c": {"mode": "chat"}, "openai/g": {"mode": "chat"}})
    gw = _gateway(
        ollama_url="",
        fallbacks=["llama3.2", "gpt/g"],
        secrets=_FakeSecrets({"llm/anthropic": {"api_key": "k"}, "llm/openai": {"api_key": "k"}}),
    )
    result = await gw.chat([ChatMessage(role="user", content="hi")], model="claude/c")

    assert result.content == "ok"
    assert calls == ["anthropic/c", "openai/g"]  # the local fallback was never tried
