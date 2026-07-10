"""Tests for CoreAppSettings — the LLM-tuning fields and blank->None coercion (#114)."""

from __future__ import annotations

import pytest

from epicurus_core_app.settings import CoreAppSettings

_TUNING_VARS = ("LLM_TEMPERATURE", "LLM_TOP_P", "LLM_NUM_CTX")


def test_tuning_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _TUNING_VARS:
        monkeypatch.delenv(var, raising=False)
    settings = CoreAppSettings(service_name="test")
    assert settings.llm_temperature is None
    assert settings.llm_top_p is None
    assert settings.llm_num_ctx is None


def test_blank_env_coerces_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Compose passes ${LLM_TEMPERATURE:-} as an empty string — it must mean "unset",
    # not a parse error.
    monkeypatch.setenv("LLM_TEMPERATURE", "")
    monkeypatch.setenv("LLM_TOP_P", "   ")
    monkeypatch.setenv("LLM_NUM_CTX", "")
    settings = CoreAppSettings(service_name="test")
    assert settings.llm_temperature is None
    assert settings.llm_top_p is None
    assert settings.llm_num_ctx is None


def test_env_values_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_TEMPERATURE", "0.7")
    monkeypatch.setenv("LLM_TOP_P", "0.9")
    monkeypatch.setenv("LLM_NUM_CTX", "8192")
    settings = CoreAppSettings(service_name="test")
    assert settings.llm_temperature == 0.7
    assert settings.llm_top_p == 0.9
    assert settings.llm_num_ctx == 8192


def test_module_hostnames_recovers_schemeless_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    # A scheme-less entry ("knowledge:8080") parses its host *as* the URL scheme, leaving
    # urlparse().hostname None — which would silently unlock that module's folder. The host is
    # recovered so the folder-lock (#479) survives a scheme-less config (#554).
    monkeypatch.delenv("MODULE_URLS", raising=False)
    settings = CoreAppSettings(service_name="test", module_urls="knowledge:8080, http://notes:8081")
    assert settings.module_hostnames == ["knowledge", "notes"]


def test_module_hostnames_skips_hostless_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    # A truly host-less entry can lock nothing and is skipped (with a logged warning), while the
    # well-formed entries still lock their folders (#554).
    monkeypatch.delenv("MODULE_URLS", raising=False)
    settings = CoreAppSettings(service_name="test", module_urls="http://knowledge:8080, /")
    assert settings.module_hostnames == ["knowledge"]
