"""Confinement: working directories and agent file paths stay inside the workspace, and agent
processes never see the service's environment."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from epicurus_hermarchus.sandbox import (
    PathError,
    build_child_env,
    confine_path,
    resolve_workdir,
)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    w = tmp_path / "workspace"
    w.mkdir()
    return w


def test_a_relative_workdir_is_created_inside(ws: Path) -> None:
    path, rel = resolve_workdir(ws, "a/b")
    assert path == (ws / "a" / "b").resolve() and path.is_dir()
    assert rel == "a/b"


def test_the_workspace_root_itself_is_allowed(ws: Path) -> None:
    path, rel = resolve_workdir(ws, ".")
    assert path == ws.resolve() and rel == "."
    assert resolve_workdir(ws, "")[1] == "."


def test_an_absolute_workdir_inside_is_accepted(ws: Path) -> None:
    _, rel = resolve_workdir(ws, str(ws / "proj"))
    assert rel == "proj"


@pytest.mark.parametrize("bad", ["..", "../x", "a/../../x", "a/..", "/etc", "x\x00y"])
def test_dot_dot_absolute_outside_and_nul_are_refused(ws: Path, bad: str) -> None:
    with pytest.raises(PathError):
        resolve_workdir(ws, bad)
    assert not (ws.parent / "x").exists()


def test_a_symlink_escape_is_refused_and_nothing_is_created_outside(
    ws: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws / "link").symlink_to(outside)
    with pytest.raises(PathError, match="outside"):
        resolve_workdir(ws, "link")
    with pytest.raises(PathError, match="outside"):
        resolve_workdir(ws, "link/new")
    assert not (outside / "new").exists(), "mkdir followed the symlink out of the workspace"


def test_a_symlink_that_stays_inside_is_fine(ws: Path) -> None:
    (ws / "real").mkdir()
    (ws / "alias").symlink_to(ws / "real")
    path, rel = resolve_workdir(ws, "alias")
    assert path == (ws / "real").resolve() and rel == "real"


def test_a_file_is_not_a_workdir(ws: Path) -> None:
    (ws / "f").write_text("x")
    with pytest.raises(PathError, match="not a directory"):
        resolve_workdir(ws, "f")


def test_confine_path_accepts_inside_and_refuses_everything_else(ws: Path, tmp_path: Path) -> None:
    assert confine_path(ws, str(ws / "x.txt")) == (ws / "x.txt").resolve()
    for bad in ("relative.txt", str(ws / ".." / "x"), "/etc/passwd", str(ws) + "/a\x00b"):
        with pytest.raises(PathError):
            confine_path(ws, bad)
    (tmp_path / "secret.txt").write_text("s")
    (ws / "s").symlink_to(tmp_path / "secret.txt")
    with pytest.raises(PathError, match="outside"):
        confine_path(ws, str(ws / "s"))
    (ws / "d").symlink_to(tmp_path)
    with pytest.raises(PathError, match="outside"):
        confine_path(ws, str(ws / "d" / "new.txt"))


def test_a_sibling_with_a_common_prefix_is_outside(ws: Path, tmp_path: Path) -> None:
    sibling = tmp_path / "workspace-evil"
    sibling.mkdir()
    with pytest.raises(PathError):
        confine_path(ws, str(sibling / "x"))


TOKEN = "the-service-token-0123456789"


def test_the_child_environment_is_built_from_an_allow_list(tmp_path: Path) -> None:
    parent = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HERMARCHUS_TOKEN": TOKEN,
        "HERMARCHUS_PROFILE": "local",
        "AWS_SECRET_ACCESS_KEY": "aws",
        "OPENBAO_TOKEN": "bao",
        "SOME_PROXY": "http://proxy",
    }
    env = build_child_env(
        agent_home=tmp_path / "home",
        parent_env=parent,
        passthrough=["SOME_PROXY"],
        kind_env={"VENDOR_FLAG": "1"},
        secrets=[TOKEN],
    )
    assert env["LANG"] == "C.UTF-8"
    assert env["SOME_PROXY"] == "http://proxy"
    assert env["VENDOR_FLAG"] == "1"
    assert env["HOME"] == str(tmp_path / "home")
    assert env["NPM_CONFIG_PREFIX"] == f"{tmp_path / 'home'}/.npm-global"
    assert env["PATH"].split(os.pathsep)[:2] == [
        f"{tmp_path / 'home'}/.local/bin",
        f"{tmp_path / 'home'}/.npm-global/bin",
    ]
    assert env["PATH"].endswith("/usr/bin:/bin")
    for leaked in (
        "HERMARCHUS_TOKEN",
        "HERMARCHUS_PROFILE",
        "AWS_SECRET_ACCESS_KEY",
        "OPENBAO_TOKEN",
    ):
        assert leaked not in env
    assert TOKEN not in env.values()


def test_the_token_is_stripped_whichever_layer_asks_for_it(tmp_path: Path) -> None:
    env = build_child_env(
        agent_home=tmp_path,
        parent_env={"PATH": "/bin", "LEAKY": TOKEN},
        passthrough=["LEAKY", "HERMARCHUS_TOKEN"],
        kind_env={"ALSO_LEAKY": TOKEN, "hermarchus_token": "x"},
        secrets=[TOKEN],
    )
    assert "LEAKY" not in env and "ALSO_LEAKY" not in env and "hermarchus_token" not in env
    assert TOKEN not in env.values()


def test_a_missing_path_gets_a_default(tmp_path: Path) -> None:
    env = build_child_env(agent_home=tmp_path, parent_env={})
    assert env["PATH"].endswith("/usr/bin:/sbin:/bin")
