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


def test_is_hosted_recognises_known_provider_prefixes() -> None:
    assert registry.is_hosted("claude/claude-3-5-sonnet-latest")
    assert registry.is_hosted("gpt/gpt-4o")
    assert registry.is_hosted("custom/my-model")


def test_is_hosted_rejects_local_ids() -> None:
    # The classification that keeps a local model out of the saved-hosted list (#496): a bare
    # name, the explicit local alias, and — the original bug — an ``hf.co/…`` prefix are all local.
    assert not registry.is_hosted("llama3.2")
    assert not registry.is_hosted("local/llama3.2")
    assert not registry.is_hosted("hf.co/org/model:tag")
    assert not registry.is_hosted("qwen2.5:0.5b")


def test_is_hosted_rejects_provider_only_id() -> None:
    # A provider prefix with no model part names a hosted *provider* but not a hosted *model*; it
    # used to be True and let a junk "claude/" row into the saved-models table (#537).
    assert not registry.is_hosted("claude/")
    assert not registry.is_hosted("gpt/")
    assert not registry.is_hosted("claude/   ")  # a whitespace-only model part is empty too
    # A real model part after the prefix is still hosted.
    assert registry.is_hosted("claude/opus-4")


def test_openrouter_alias_routes_to_litellms_openrouter_provider() -> None:
    # First-class rather than routed through `custom` (#865): it keeps the single generic
    # OpenAI-compatible slot free and lets LiteLLM's own openrouter dispatch (and its
    # context/cost map) apply.
    litellm_model, provider = registry.resolve("openrouter/anthropic/claude-sonnet-4.6")
    assert litellm_model == "openrouter/anthropic/claude-sonnet-4.6"
    assert not provider.is_local
    assert provider.secret_path == "llm/openrouter"
    assert not provider.needs_base_url


def test_only_the_first_slash_separates_alias_from_model() -> None:
    # An aggregator names models with a slash of their own, so the id has two. Splitting on
    # every slash would hand LiteLLM a truncated model name; `partition` keeps it whole.
    litellm_model, provider = registry.resolve("openrouter/openai/text-embedding-3-small")
    assert litellm_model == "openrouter/openai/text-embedding-3-small"
    assert provider.secret_path == "llm/openrouter"
    assert registry.is_hosted("openrouter/openai/text-embedding-3-small")
    # A slash inside the model part of any provider survives the same way.
    assert registry.resolve("custom/org/model")[0] == "openai/org/model"


def test_is_hosted_still_rejects_a_bare_openrouter_prefix() -> None:
    assert not registry.is_hosted("openrouter/")
    assert not registry.is_hosted("openrouter/  ")
