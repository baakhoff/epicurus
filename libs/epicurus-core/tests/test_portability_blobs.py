"""The blob half of the portability contract (#876) — the helper's three extra routes.

The record half proves itself over a dict; this proves itself over a dict of *bytes*. What is
under test is the transport and the two rules that make an archive of objects safe to apply
twice: the routes exist only for a store that has bytes at all, and a body that does not match
the digest it was sent with never becomes an object.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from epicurus_core.module import EpicurusModule
from epicurus_core.portability import (
    BlobOutcome,
    BlobPortabilityStore,
    BlobRef,
    ImportReport,
    PortabilityRecord,
    add_portability_routes,
    verified_chunks,
)

TENANT = "local"


class RowsOnlyStore:
    """A store with records and no bytes — the shape every other module has."""

    schema = "widgets/1"

    async def export(self, *, tenant_id: str) -> AsyncIterator[PortabilityRecord]:
        return
        yield  # pragma: no cover - an empty async generator

    async def import_(
        self, *, tenant_id: str, records: AsyncIterator[PortabilityRecord], dry_run: bool
    ) -> ImportReport:
        return ImportReport(schema_name=self.schema)


class BlobStore(RowsOnlyStore):
    """Records plus bytes, held in a dict keyed by ``(tenant, id)``."""

    schema = "widgets/1"

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.seen_tenants: list[str] = []

    async def blobs(self, *, tenant_id: str) -> AsyncIterator[BlobRef]:
        self.seen_tenants.append(tenant_id)
        for (tenant, blob_id), (data, media) in sorted(self.objects.items()):
            if tenant == tenant_id:
                yield BlobRef(id=blob_id, size=len(data), content_type=media)

    async def open_blob(self, *, tenant_id: str, blob_id: str) -> AsyncIterator[bytes]:
        stored = self.objects.get((tenant_id, blob_id))
        if stored is None:
            raise FileNotFoundError(blob_id)
        data = stored[0]
        for start in range(0, max(len(data), 1), 8):
            chunk = data[start : start + 8]
            if chunk:
                yield chunk

    async def put_blob(
        self,
        *,
        tenant_id: str,
        blob_id: str,
        sha256: str,
        size: int,
        content_type: str,
        chunks: AsyncIterator[bytes],
    ) -> BlobOutcome:
        self.seen_tenants.append(tenant_id)
        # Consume first, publish after — the library's verification fires at end of stream,
        # so anything written before that could be written from bytes that never validated.
        body = b"".join([chunk async for chunk in chunks])
        existing = self.objects.get((tenant_id, blob_id))
        if existing is not None:
            if hashlib.sha256(existing[0]).hexdigest() == sha256:
                return BlobOutcome(id=blob_id, outcome="skipped")
            return BlobOutcome(id=blob_id, outcome="skipped", warning=f"{blob_id} differs")
        self.objects[(tenant_id, blob_id)] = (body, content_type)
        return BlobOutcome(id=blob_id, outcome="created")


def _client(store: object) -> AsyncClient:
    module = EpicurusModule("widgets", version="1.0.0", portable=True)
    app = FastAPI()
    add_portability_routes(app, module, store)  # type: ignore[arg-type]
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://module")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── discovery: the blob half is optional ──────────────────────────────────────


def test_a_rows_only_store_is_not_a_blob_store() -> None:
    """``isinstance`` is the whole of the discovery, so it had better be honest."""
    assert not isinstance(RowsOnlyStore(), BlobPortabilityStore)
    assert isinstance(BlobStore(), BlobPortabilityStore)


async def test_a_module_without_bytes_serves_no_blob_routes() -> None:
    """404, not 500: the core reads it as "this module has no blob half" and moves on."""
    async with _client(RowsOnlyStore()) as client:
        listing = await client.get("/export/blobs", params={"tenant_id": TENANT})
        assert listing.status_code == 404
        put = await client.put(
            "/import/blobs/x", params={"tenant_id": TENANT, "sha256": "", "size": 0}, content=b"hi"
        )
        assert put.status_code == 404


# ── export ────────────────────────────────────────────────────────────────────


async def test_the_listing_is_ndjson_with_no_header_and_is_tenant_scoped() -> None:
    store = BlobStore()
    store.objects[(TENANT, "uploads/a.pdf")] = (b"alpha", "application/pdf")
    store.objects[("other", "uploads/secret.pdf")] = (b"not yours", "application/pdf")
    async with _client(store) as client:
        response = await client.get("/export/blobs", params={"tenant_id": TENANT})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        refs = [BlobRef.model_validate_json(line) for line in response.text.splitlines() if line]
    assert [r.id for r in refs] == ["uploads/a.pdf"]
    assert refs[0].size == 5
    assert refs[0].content_type == "application/pdf"
    assert store.seen_tenants == [TENANT]


async def test_a_blob_streams_back_byte_for_byte_including_a_slashed_id() -> None:
    store = BlobStore()
    payload = bytes(range(256)) * 40
    store.objects[(TENANT, "uploads/deep/nested name.bin")] = (payload, "application/octet-stream")
    async with _client(store) as client:
        response = await client.get(
            "/export/blobs/uploads/deep/nested%20name.bin", params={"tenant_id": TENANT}
        )
    assert response.status_code == 200
    assert response.content == payload


async def test_a_missing_blob_is_a_404_and_not_a_truncated_download() -> None:
    """The status has to be decided before the response starts, or it cannot be corrected."""
    async with _client(BlobStore()) as client:
        response = await client.get("/export/blobs/nope", params={"tenant_id": TENANT})
    assert response.status_code == 404
    assert "nope" in response.json()["detail"]


async def test_an_empty_blob_exports_as_an_empty_body_not_a_404() -> None:
    store = BlobStore()
    store.objects[(TENANT, "empty")] = (b"", "text/plain")
    async with _client(store) as client:
        response = await client.get("/export/blobs/empty", params={"tenant_id": TENANT})
    assert response.status_code == 200
    assert response.content == b""


async def test_the_blob_routes_require_a_tenant() -> None:
    async with _client(BlobStore()) as client:
        assert (await client.get("/export/blobs")).status_code == 400
        assert (await client.get("/export/blobs/x")).status_code == 400
        assert (await client.put("/import/blobs/x", content=b"")).status_code == 400


# ── import ────────────────────────────────────────────────────────────────────


async def test_a_blob_is_written_once_and_a_second_apply_is_a_no_op() -> None:
    store = BlobStore()
    payload = b"a report worth carrying" * 100
    params: dict[str, str | int] = {
        "tenant_id": TENANT,
        "sha256": _sha(payload),
        "size": len(payload),
        "content_type": "text/plain",
    }
    async with _client(store) as client:
        first = await client.put("/import/blobs/uploads/r.txt", params=params, content=payload)
        second = await client.put("/import/blobs/uploads/r.txt", params=params, content=payload)
    assert first.json() == {"id": "uploads/r.txt", "outcome": "created", "warning": None}
    assert second.json()["outcome"] == "skipped"
    assert second.json()["warning"] is None
    assert store.objects[(TENANT, "uploads/r.txt")] == (payload, "text/plain")


async def test_differing_bytes_are_never_overwritten_and_are_named() -> None:
    store = BlobStore()
    store.objects[(TENANT, "uploads/r.txt")] = (b"the operator's own edit", "text/plain")
    incoming = b"the archive's version"
    async with _client(store) as client:
        response = await client.put(
            "/import/blobs/uploads/r.txt",
            params={"tenant_id": TENANT, "sha256": _sha(incoming), "size": len(incoming)},
            content=incoming,
        )
    body = response.json()
    assert body["outcome"] == "skipped"
    assert "differs" in body["warning"]
    assert store.objects[(TENANT, "uploads/r.txt")][0] == b"the operator's own edit"


async def test_a_body_that_does_not_match_its_digest_is_refused() -> None:
    store = BlobStore()
    async with _client(store) as client:
        response = await client.put(
            "/import/blobs/uploads/r.txt",
            params={"tenant_id": TENANT, "sha256": _sha(b"expected"), "size": len(b"corrupted")},
            content=b"corrupted",
        )
    assert response.status_code == 400
    assert "digest mismatch" in response.json()["detail"]
    assert store.objects == {}


async def test_a_body_of_the_wrong_length_is_refused() -> None:
    store = BlobStore()
    payload = b"short"
    async with _client(store) as client:
        response = await client.put(
            "/import/blobs/x",
            params={"tenant_id": TENANT, "sha256": _sha(payload), "size": 999},
            content=payload,
        )
    assert response.status_code == 400
    assert "size mismatch" in response.json()["detail"]
    assert store.objects == {}


async def test_the_import_defaults_the_id_when_the_store_leaves_it_blank() -> None:
    class TerseStore(BlobStore):
        async def put_blob(
            self,
            *,
            tenant_id: str,
            blob_id: str,
            sha256: str,
            size: int,
            content_type: str,
            chunks: AsyncIterator[bytes],
        ) -> BlobOutcome:
            async for _ in chunks:
                pass
            return BlobOutcome(outcome="created")  # deliberately no id

    async with _client(TerseStore()) as client:
        response = await client.put(
            "/import/blobs/uploads/x.bin",
            params={"tenant_id": TENANT, "sha256": "", "size": 0},
            content=b"bytes",
        )
    assert response.json()["id"] == "uploads/x.bin"


# ── verified_chunks ───────────────────────────────────────────────────────────


async def test_verified_chunks_passes_bytes_through_untouched() -> None:
    async def source() -> AsyncIterator[bytes]:
        for part in (b"one ", b"two ", b"three"):
            yield part

    seen = [chunk async for chunk in verified_chunks(source(), sha256=_sha(b"one two three"))]
    assert b"".join(seen) == b"one two three"


async def test_verified_chunks_only_raises_after_the_last_chunk() -> None:
    """Which is why the contract asks a store to publish nothing until the stream ends."""

    async def source() -> AsyncIterator[bytes]:
        yield b"first"
        yield b"second"

    seen: list[bytes] = []
    try:
        async for chunk in verified_chunks(source(), sha256=_sha(b"something else")):
            seen.append(chunk)
    except ValueError as exc:
        assert "digest mismatch" in str(exc)
    else:  # pragma: no cover - the mismatch must raise
        raise AssertionError("a mismatched digest must raise")
    assert seen == [b"first", b"second"]


async def test_verified_chunks_with_no_declared_digest_checks_only_the_size() -> None:
    async def source() -> AsyncIterator[bytes]:
        yield b"12345"

    assert [c async for c in verified_chunks(source(), sha256="", size=5)] == [b"12345"]
