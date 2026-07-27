"""Tests for external-mount config parsing and store construction (#731)."""

from __future__ import annotations

from pathlib import Path

import pytest

from epicurus_core.files import LocalFileStore
from epicurus_core_app.mounts import MountSpec, build_mounts, parse_mount_specs

# ── parse_mount_specs ───────────────────────────────────────────────────────────


def test_parses_single_ro_default() -> None:
    specs = parse_mount_specs(mounts="media:/mnt/spaces/media")
    assert specs == [
        MountSpec(
            name="media",
            path=Path("/mnt/spaces/media"),
            read_only=True,
            indexed=False,
            exclude=(),
        )
    ]


def test_parses_explicit_rw() -> None:
    specs = parse_mount_specs(mounts="docs:/mnt/spaces/docs:rw")
    assert specs[0].read_only is False


def test_parses_explicit_ro() -> None:
    specs = parse_mount_specs(mounts="docs:/mnt/spaces/docs:ro")
    assert specs[0].read_only is True


def test_parses_multiple_comma_separated() -> None:
    specs = parse_mount_specs(mounts="media:/mnt/media:ro,docs:/mnt/docs:rw")
    assert [s.name for s in specs] == ["media", "docs"]
    assert [s.read_only for s in specs] == [True, False]


def test_blank_entries_and_whitespace_tolerated() -> None:
    specs = parse_mount_specs(mounts=" media:/mnt/media:ro , , docs:/mnt/docs:rw ")
    assert [s.name for s in specs] == ["media", "docs"]


def test_empty_mounts_yields_no_specs() -> None:
    assert parse_mount_specs(mounts="") == []
    assert parse_mount_specs(mounts="   ") == []


def test_indexed_flag_set_from_second_setting() -> None:
    specs = parse_mount_specs(mounts="media:/mnt/media:ro,docs:/mnt/docs:rw", indexed="docs")
    by_name = {s.name: s for s in specs}
    assert by_name["docs"].indexed is True
    assert by_name["media"].indexed is False


def test_exclude_parsed_per_mount() -> None:
    specs = parse_mount_specs(
        mounts="media:/mnt/media:ro,docs:/mnt/docs:rw",
        indexed="media,docs",
        exclude="media=*.tmp|.cache/*;docs=node_modules/*",
    )
    by_name = {s.name: s for s in specs}
    assert by_name["media"].exclude == ("*.tmp", ".cache/*")
    assert by_name["docs"].exclude == ("node_modules/*",)


@pytest.mark.parametrize(
    "entry",
    [
        "just-a-name",  # no colon at all
        "name:path:ro:extra",  # too many segments
        "Media:/mnt/media",  # uppercase not allowed
        "med ia:/mnt/media",  # space not allowed
        "-media:/mnt/media",  # cannot start with '-'
        "media:/mnt/media:rwx",  # invalid mode
        "media:",  # empty path
        ":/mnt/media",  # empty name
    ],
)
def test_malformed_entry_rejected(entry: str) -> None:
    with pytest.raises(ValueError):
        parse_mount_specs(mounts=entry)


def test_duplicate_name_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        parse_mount_specs(mounts="media:/mnt/a:ro,media:/mnt/b:ro")


def test_indexed_unknown_name_rejected() -> None:
    with pytest.raises(ValueError, match="files_external_mounts_indexed"):
        parse_mount_specs(mounts="media:/mnt/media:ro", indexed="ghost")


def test_exclude_unknown_name_rejected() -> None:
    with pytest.raises(ValueError, match="files_external_mounts_exclude"):
        parse_mount_specs(mounts="media:/mnt/media:ro", exclude="ghost=*.tmp")


def test_exclude_malformed_block_rejected() -> None:
    with pytest.raises(ValueError):
        parse_mount_specs(mounts="media:/mnt/media:ro", exclude="media")  # no '='


def test_long_name_boundary() -> None:
    ok_name = "a" * 63
    too_long = "a" * 64
    assert parse_mount_specs(mounts=f"{ok_name}:/mnt/x")[0].name == ok_name
    with pytest.raises(ValueError):
        parse_mount_specs(mounts=f"{too_long}:/mnt/x")


# ── build_mounts ─────────────────────────────────────────────────────────────


def test_build_mounts_constructs_one_store_per_spec(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    specs = [
        MountSpec(name="a", path=a, read_only=True, indexed=False, exclude=()),
        MountSpec(name="b", path=b, read_only=False, indexed=True, exclude=("*.tmp",)),
    ]
    mounts = build_mounts(specs)
    assert set(mounts) == {"a", "b"}
    assert isinstance(mounts["a"].store, LocalFileStore)
    assert mounts["a"].read_only is True
    assert mounts["b"].read_only is False
    assert mounts["b"].indexed is True
    assert mounts["b"].exclude == ("*.tmp",)


async def test_build_mounts_store_addresses_its_own_root(tmp_path: Path) -> None:
    """The constructed store is tenant_subdir=False — it addresses *path* directly."""
    root = tmp_path / "media"
    root.mkdir()
    specs = [MountSpec(name="media", path=root, read_only=False, indexed=False, exclude=())]
    mount = build_mounts(specs)["media"]
    await mount.store.write_text(tenant="local", path="hello.txt", content="hi")
    assert (root / "hello.txt").read_text(encoding="utf-8") == "hi"
    assert not (root / "local").exists()


def test_empty_specs_yields_empty_mounts() -> None:
    assert build_mounts([]) == {}
