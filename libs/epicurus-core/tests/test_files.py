"""Tests for the core-owned file space (ADR-0052): path-safety, the local backend, the factory.

The S3 backend round-trip is covered under the ``integration`` marker (testcontainers/MinIO);
the local backend and path-safety are pure unit tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from epicurus_core.files import (
    LocalFileStore,
    S3FileStore,
    build_file_store,
    normalize_rel,
)
from epicurus_core.tenancy import TenantError

TENANT = "test"


# ── normalize_rel ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a/b.txt", "a/b.txt"),
        ("/a//b/./c.txt", "a/b/c.txt"),
        ("a\\b\\c.txt", "a/b/c.txt"),
        ("", ""),
        ("/", ""),
        ("./", ""),
    ],
)
def test_normalize_rel_cleans_paths(raw: str, expected: str) -> None:
    assert normalize_rel(raw) == expected


@pytest.mark.parametrize("raw", ["../etc/passwd", "a/../../b", "..", "a/.."])
def test_normalize_rel_rejects_traversal(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_rel(raw)


# ── LocalFileStore ────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> LocalFileStore:
    return LocalFileStore(tmp_path)


async def test_write_then_read_round_trips(store: LocalFileStore) -> None:
    entry = await store.write_text(tenant=TENANT, path="notes/hello.md", content="hi")
    assert entry.path == "notes/hello.md"
    assert entry.name == "hello.md"
    assert entry.kind == "file"
    assert entry.size == len(b"hi")
    assert await store.read_text(tenant=TENANT, path="notes/hello.md") == "hi"


async def test_write_creates_parent_dirs(store: LocalFileStore, tmp_path: Path) -> None:
    await store.write_text(tenant=TENANT, path="a/b/c.txt", content="x")
    assert (tmp_path / TENANT / "a" / "b" / "c.txt").read_text() == "x"


async def test_read_missing_raises(store: LocalFileStore) -> None:
    with pytest.raises(FileNotFoundError):
        await store.read_bytes(tenant=TENANT, path="nope.txt")


async def test_list_dir_root_and_subdir(store: LocalFileStore) -> None:
    await store.write_text(tenant=TENANT, path="docs/a.md", content="a")
    await store.write_text(tenant=TENANT, path="docs/b.md", content="b")
    await store.write_text(tenant=TENANT, path="top.txt", content="t")
    root = await store.list_dir(tenant=TENANT, path="")
    assert {(e.name, e.kind) for e in root} == {("docs", "dir"), ("top.txt", "file")}
    assert root[0].kind == "dir"  # directories sort before files
    docs = await store.list_dir(tenant=TENANT, path="docs")
    assert {e.name for e in docs} == {"a.md", "b.md"}


async def test_stat_and_exists(store: LocalFileStore) -> None:
    await store.write_text(tenant=TENANT, path="f.txt", content="x")
    entry = await store.stat(tenant=TENANT, path="f.txt")
    assert entry is not None and entry.kind == "file"
    assert await store.exists(tenant=TENANT, path="f.txt")
    assert await store.stat(tenant=TENANT, path="ghost.txt") is None
    assert not await store.exists(tenant=TENANT, path="ghost.txt")


async def test_delete_file_and_tree(store: LocalFileStore) -> None:
    await store.write_text(tenant=TENANT, path="dir/a.txt", content="a")
    await store.write_text(tenant=TENANT, path="dir/sub/b.txt", content="b")
    assert await store.delete(tenant=TENANT, path="dir/a.txt") is True
    assert await store.delete(tenant=TENANT, path="dir/a.txt") is False  # already gone
    assert await store.delete(tenant=TENANT, path="dir") is True  # whole subtree
    assert await store.list_dir(tenant=TENANT, path="") == []


async def test_delete_and_write_root_rejected(store: LocalFileStore) -> None:
    with pytest.raises(ValueError):
        await store.delete(tenant=TENANT, path="")
    with pytest.raises(ValueError):
        await store.write_text(tenant=TENANT, path="", content="x")


async def test_ensure_dir_and_tenant_root(store: LocalFileStore, tmp_path: Path) -> None:
    entry = await store.ensure_dir(tenant=TENANT, path="projects")
    assert entry.kind == "dir" and entry.path == "projects"
    assert (tmp_path / TENANT / "projects").is_dir()
    await store.ensure_tenant_root(tenant=TENANT)
    assert (tmp_path / TENANT).is_dir()


async def test_store_rejects_traversal(store: LocalFileStore) -> None:
    with pytest.raises(ValueError):
        await store.read_bytes(tenant=TENANT, path="../escape.txt")


async def test_tenant_isolation(store: LocalFileStore) -> None:
    await store.write_text(tenant="tenant-a", path="secret.txt", content="a")
    assert await store.stat(tenant="tenant-b", path="secret.txt") is None
    assert await store.list_dir(tenant="tenant-b", path="") == []


async def test_invalid_tenant_rejected(store: LocalFileStore) -> None:
    with pytest.raises(TenantError):
        await store.write_text(tenant="Bad Tenant!", path="x.txt", content="x")


async def test_read_text_size_cap(store: LocalFileStore) -> None:
    big = "x" * (256 * 1024 + 1)
    await store.write_text(tenant=TENANT, path="big.txt", content=big)
    with pytest.raises(ValueError):
        await store.read_text(tenant=TENANT, path="big.txt")
    # The raw byte API has no cap.
    assert len(await store.read_bytes(tenant=TENANT, path="big.txt")) == len(big.encode())


async def test_read_text_binary_raises(store: LocalFileStore) -> None:
    await store.write_bytes(tenant=TENANT, path="blob.bin", data=b"\xff\xfe\x00")
    with pytest.raises(UnicodeDecodeError):
        await store.read_text(tenant=TENANT, path="blob.bin")


# ── build_file_store ──────────────────────────────────────────────────────────


def test_build_local(tmp_path: Path) -> None:
    assert isinstance(build_file_store(backend="local", root=tmp_path), LocalFileStore)


def test_build_s3_requires_creds() -> None:
    with pytest.raises(ValueError):
        build_file_store(backend="s3")


def test_build_s3_constructs() -> None:
    store = build_file_store(backend="s3", s3_url="http://x", s3_access_key="k", s3_secret_key="s")
    assert isinstance(store, S3FileStore)


# ── S3FileStore (integration: requires Docker/MinIO) ──────────────────────────


@pytest.mark.integration
async def test_s3_round_trip() -> None:
    from testcontainers.minio import MinioContainer  # type: ignore[import-untyped]

    with MinioContainer() as minio:
        store = S3FileStore(
            url=f"http://{minio.get_container_host_ip()}:{minio.get_exposed_port(9000)}",
            access_key=minio.access_key,
            secret_key=minio.secret_key,
        )
        await store.write_text(tenant=TENANT, path="docs/a.md", content="hello")
        assert await store.read_text(tenant=TENANT, path="docs/a.md") == "hello"

        root = await store.list_dir(tenant=TENANT, path="")
        assert any(e.name == "docs" and e.kind == "dir" for e in root)
        docs = await store.list_dir(tenant=TENANT, path="docs")
        assert any(e.name == "a.md" and e.kind == "file" for e in docs)

        stat = await store.stat(tenant=TENANT, path="docs/a.md")
        assert stat is not None and stat.kind == "file"

        assert await store.delete(tenant=TENANT, path="docs") is True
        assert await store.list_dir(tenant=TENANT, path="") == []
