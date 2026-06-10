"""Unit tests for the provider registry (model string -> LiteLLM + provider)."""

from __future__ import annotations

from epicurus_core_app.llm import providers as registry


def test_bare_name_routes_to_local() -> None:
    litellm_model, provider = registry.resolve("llama3.2")
    assert litellm_model == "ollama_chat/llama3.2"
    assert provider.is_local


def test_ollama_tag_routes_to_local() -> None:
    litellm_model, provider = registry.resolve("qwen2.5:0.5b")
    assert litellm_model == "ollama_chat/qwen2.5:0.5b"
    assert provider.is_local


def test_local_alias_routes_to_local() -> None:
    litellm_model, provider = registry.resolve("local/llama3.2")
    assert litellm_model == "ollama_chat/llama3.2"
    assert provider.is_local


def test_claude_alias_routes_to_anthropic() -> None:
    litellm_model, provider = registry.resolve("claude/claude-3-5-sonnet-latest")
    assert litellm_model == "anthropic/claude-3-5-sonnet-latest"
    assert not provider.is_local
    assert provider.secret_path == "llm/anthropic"


def test_custom_alias_needs_base_url() -> None:
    litellm_model, provider = registry.resolve("custom/my-model")
    assert litellm_model == "openai/my-model"
    assert provider.needs_base_url
    assert provider.secret_path == "llm/custom"


def test_unknown_prefix_falls_back_to_local() -> None:
    # An unknown "provider" prefix is treated as a bare local model name — callers use
    # the friendly epicurus aliases (claude, gpt, ...), not LiteLLM's own prefixes.
    litellm_model, provider = registry.resolve("anthropic/foo")
    assert provider.is_local
    assert litellm_model == "ollama_chat/anthropic/foo"
