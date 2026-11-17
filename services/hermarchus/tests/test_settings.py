"""Startup refusals: no token, a weak token, a bad profile, a pass-through of our own settings."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import epicurus_hermarchus
from epicurus_hermarchus.settings import HermarchusSettings, SettingsError

ROOT = Path(__file__).resolve().parents[1]


def _settings(**kw: object) -> HermarchusSettings:
    return HermarchusSettings(**kw)  # type: ignore[arg-type]


def test_refuses_to_start_without_a_token() -> None:
    with pytest.raises(SettingsError, match="no bearer token"):
        _settings(token=None).validated()


def test_refuses_a_short_token() -> None:
    with pytest.raises(SettingsError, match="shorter than 16"):
        _settings(token="short").validated()


def test_reads_the_token_from_a_file(tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text("from-a-file-0123456789\n")
    s = _settings(token=None, token_file=f).validated()
    assert s.bearer_token() == "from-a-file-0123456789"


def test_an_unreadable_token_file_is_named(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="cannot be read"):
        _settings(token=None, token_file=tmp_path / "missing").validated()


@pytest.mark.parametrize("profile", ["", "Upper", "-lead", "a" * 64, "has space"])
def test_the_bound_profile_is_validated_with_the_core_rule(profile: str) -> None:
    with pytest.raises(SettingsError, match="HERMARCHUS_PROFILE"):
        _settings(token="x" * 20, profile=profile).validated()


def test_a_passthrough_may_not_name_hermarchus_settings() -> None:
    with pytest.raises(SettingsError, match="PASSTHROUGH"):
        _settings(token="x" * 20, child_env_passthrough="LANG,HERMARCHUS_TOKEN").validated()


def test_the_entrypoint_exits_non_zero_without_a_token(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMARCHUS_")}
    env["HERMARCHUS_STATE_DIR"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "epicurus_hermarchus"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )
    assert proc.returncode == 2
    assert "refusing to start" in proc.stderr
    assert "HERMARCHUS_TOKEN" in proc.stderr


def test_the_package_version_matches_its_manifest() -> None:
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert epicurus_hermarchus.__version__ == manifest["project"]["version"] == "0.1.0"
