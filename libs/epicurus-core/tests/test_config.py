"""Tests for CoreSettings loading and derived behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from epicurus_core.config import CoreSettings


def test_defaults() -> None:
    s = CoreSettings()
    assert s.service_name == "epicurus"
    assert s.app_env == "local"
    assert s.log_level == "info"
    assert s.default_tenant_id == "local"
    assert s.is_production is False
    assert s.use_json_logs is False  # local -> console


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SERVICE_NAME", "agent")
    s = CoreSettings()
    assert s.app_env == "production"
    assert s.service_name == "agent"
    assert s.is_production is True
    assert s.use_json_logs is True  # production -> json


def test_json_logs_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JSON_LOGS", "false")
    assert CoreSettings().use_json_logs is False


def test_invalid_default_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_TENANT_ID", "Bad_Tenant")
    with pytest.raises(ValidationError):
        CoreSettings()


def test_resolve_openbao_token_prefers_explicit(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("from-file\n", encoding="utf-8")
    s = CoreSettings(openbao_token="explicit", openbao_token_file=str(token_file))
    assert s.resolve_openbao_token() == "explicit"


def test_resolve_openbao_token_reads_file(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("  from-file\n", encoding="utf-8")
    s = CoreSettings(openbao_token=None, openbao_token_file=str(token_file))
    assert s.resolve_openbao_token() == "from-file"


def test_resolve_openbao_token_default_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENBAO_TOKEN", raising=False)
    monkeypatch.delenv("OPENBAO_TOKEN_FILE", raising=False)
    assert CoreSettings().resolve_openbao_token() is None
