"""The agent-kind registry: the built-in echo kind, the three documented vendor entries, and
operator overrides."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from epicurus_hermarchus.kinds import ECHO_KIND, KindError, load_kinds


def test_echo_is_built_in_first_and_runs_with_this_python() -> None:
    kinds = load_kinds()
    assert next(iter(kinds)) == ECHO_KIND
    echo = kinds[ECHO_KIND]
    assert echo.builtin and echo.verified
    assert echo.argv()[1:] == ["-m", "epicurus_hermarchus.echo_agent"]
    assert echo.unavailable_reason("/nonexistent") is None  # an absolute command


def test_the_three_documented_vendor_kinds_ship_unverified() -> None:
    kinds = load_kinds()
    expected = {
        "claude-code": ("claude-agent-acp", ()),
        "codex": ("codex-acp", ()),
        "grok-build": ("grok", ("agent", "stdio")),
    }
    for name, (command, args) in expected.items():
        kind = kinds[name]
        assert (kind.command, kind.args) == (command, args)
        assert kind.install, f"{name} has no install command"
        assert kind.docs and all(d.startswith("https://") for d in kind.docs)
        assert kind.verified is False, f"{name} must not claim to be verified by running"
        assert not kind.builtin
    assert kinds["claude-code"].env == {"CLAUDE_AGENT_ACP_PROTOCOL": "v1"}


def test_an_absent_command_is_unavailable_with_a_reason_naming_the_install() -> None:
    kind = load_kinds()["codex"]
    reason = kind.unavailable_reason("/nonexistent-dir")
    assert reason is not None
    assert "codex-acp" in reason and "npm install -g @agentclientprotocol/codex-acp" in reason


def test_a_command_on_the_path_is_available(tmp_path: Path) -> None:
    exe = tmp_path / "codex-acp"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    assert load_kinds()["codex"].unavailable_reason(str(tmp_path)) is None


def test_an_operator_file_adds_replaces_and_disables(tmp_path: Path) -> None:
    f = tmp_path / "kinds.yaml"
    f.write_text(
        """
kinds:
  codex:
    enabled: false
  claude-code:
    command: /opt/claude/claude-agent-acp
    args: ["--flag"]
    env: {EXTRA: "1"}
  mine:
    name: My agent
    command: my-agent
"""
    )
    kinds = load_kinds(f)
    assert "codex" not in kinds
    assert kinds["claude-code"].command == "/opt/claude/claude-agent-acp"
    assert kinds["claude-code"].args == ("--flag",)
    assert kinds["claude-code"].verified is True  # the operator's own entry
    assert kinds["mine"].name == "My agent"
    assert next(iter(kinds)) == ECHO_KIND


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("kinds:\n  echo:\n    command: x\n", "echo"),
        ("kinds:\n  echo: null\n", "echo"),
        ("kinds:\n  Bad Name:\n    command: x\n", "kind name"),
        ("kinds:\n  x: {}\n", "command"),
        ("kinds:\n  x:\n    command: y\n    env: [1]\n", "env"),
        ("kinds:\n  x:\n    command: y\n    env: {HERMARCHUS_TOKEN: z}\n", "own settings"),
        ("kinds: [1]\n", "mapping"),
        ("kinds:\n  x: [\n", "YAML"),
    ],
)
def test_a_bad_operator_file_is_refused_with_its_reason(
    tmp_path: Path, text: str, match: str
) -> None:
    f = tmp_path / "kinds.yaml"
    f.write_text(text)
    with pytest.raises(KindError, match=match):
        load_kinds(f)


def test_a_missing_operator_file_is_named(tmp_path: Path) -> None:
    with pytest.raises(KindError, match="HERMARCHUS_KINDS_FILE"):
        load_kinds(tmp_path / "nope.yaml")
