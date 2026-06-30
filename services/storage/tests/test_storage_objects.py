"""Integration tests for the MinIO object store (requires Docker).

Run only when the integration marker is selected:

    uv run pytest -m integration services/storage/tests/test_storage_objects.py
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from testcontainers.minio import MinioContainer  # type: ignore[import-untyped]

from epicurus_storage.object_store import ObjectStore

TENANT = "test"


@pytest.fixture(scope="module")
def minio_store() -> Iterator[ObjectStore]:
    with MinioContainer() as minio:
        yield ObjectStore(
            url=f"http://{minio.get_container_host_ip()}:{minio.get_exposed_port(9000)}",
            access_key=minio.access_key,
            secret_key=minio.secret_key,
        )


@pytest.mark.integration
async def test_object_round_trip(minio_store: ObjectStore) -> None:
    """Put an object then get it back — the full MinIO round-trip."""
    await minio_store.put(tenant=TENANT, key="hello.txt", content="hello world")
    result = await minio_store.get(tenant=TENANT, key="hello.txt")
    assert result == "hello world"


@pytest.mark.integration
async def test_object_missing_returns_none(minio_store: ObjectStore) -> None:
    result = await minio_store.get(tenant=TENANT, key="does-not-exist.txt")
    assert result is None


@pytest.mark.integration
async def test_object_overwrite(minio_store: ObjectStore) -> None:
    await minio_store.put(tenant=TENANT, key="overwrite.txt", content="v1")
    await minio_store.put(tenant=TENANT, key="overwrite.txt", content="v2")
    result = await minio_store.get(tenant=TENANT, key="overwrite.txt")
    assert result == "v2"


@pytest.mark.integration
async def test_tenant_isolation(minio_store: ObjectStore) -> None:
    await minio_store.put(tenant="tenant-a", key="secret.txt", content="private")
    result = await minio_store.get(tenant="tenant-b", key="secret.txt")
    assert result is None


@pytest.mark.integration
async def test_binary_round_trip_preserves_bytes_and_content_type(
    minio_store: ObjectStore,
) -> None:
    """The chat upload sink path: arbitrary bytes + their media type survive a round-trip."""
    blob = bytes(range(256))
    await minio_store.put_bytes(
        tenant=TENANT, key="uploads/a-photo.jpg", data=blob, content_type="image/jpeg"
    )
    stored = await minio_store.get_object(tenant=TENANT, key="uploads/a-photo.jpg")
    assert stored is not None
    assert stored.data == blob
    assert stored.content_type == "image/jpeg"


@pytest.mark.integration
async def test_get_object_missing_returns_none(minio_store: ObjectStore) -> None:
    assert await minio_store.get_object(tenant=TENANT, key="uploads/nope.bin") is None
