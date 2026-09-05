"""Module data portability — the module half of the tenant export/import contract (#867).

An operator must be able to lift one tenant out of an epicurus and set it down in
another. The core assembles that archive, but only a module knows what its own
source-of-truth data *is* — so the contract is shaped exactly like ``reindexable``
(#332): a manifest flag the module raises, a pair of routes the shared library serves
for it, and a fan-out the core drives.

A module opts in with ``EpicurusModule(portable=True)`` and one call::

    store = MyPortabilityStore(...)          # implements PortabilityStore
    add_portability_routes(app, module, store)

which serves:

* ``GET  /export?tenant_id=…`` — NDJSON. A **header line**
  (``{"schema": "<module>/<n>", "component_version": "<module version>"}``) followed by one
  :class:`PortabilityRecord` per line.
* ``POST /import?tenant_id=…&dry_run=…`` — the same stream back, answering with an
  :class:`ImportReport` (per-kind ``created``/``updated``/``skipped`` counts + warnings).

A module whose data is **bytes** rather than rows (``storage``'s object bucket — #876) also
implements :class:`BlobPortabilityStore`, and the same call serves three more routes for it:

* ``GET /export/blobs?tenant_id=…`` — NDJSON, one :class:`BlobRef` per line, no header.
* ``GET /export/blobs/{id}?tenant_id=…`` — that blob's bytes, streamed.
* ``PUT /import/blobs/{id}?tenant_id=…&sha256=…&size=…&content_type=…`` — the bytes back,
  answering with a :class:`BlobOutcome`.

The blob half is **optional and discovered, not declared**: the manifest flag stays ``portable``
and the core learns a module has bytes from ``GET /export/blobs`` answering 200 rather than 404.
So a store that has no bytes is untouched by any of this, and a store that does is not asked to
restate the fact in its manifest.

Three rules the helper enforces so every module behaves the same way, whoever wrote it:

* **Additive, never destructive.** A store upserts by stable id; nothing in this contract
  can delete. Re-applying the same archive is a no-op — the counts come back
  ``skipped``/``updated`` and no row is duplicated.
* **Tenant threads through every call** (constraint #1). Neither route has a default tenant
  hiding in it; the core always names one.
* **Schema compatibility is decided before a byte is written.** A stream from a *newer*
  schema of the same module is refused (409) rather than half-applied; an *older* one is
  accepted with a warning, and the module's store owns any field-level migration.

Secrets never travel this way. A module holds no credentials to begin with (ADR-0010/0020),
and a store that put one in a record would be putting it in the operator's archive — so
don't: the core reports what to re-enter instead.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal, Protocol, runtime_checkable

from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from epicurus_core.logging import get_logger
from epicurus_core.module import EpicurusModule
from epicurus_core.tenancy import TenantError, validate_tenant_id

__all__ = [
    "BLOB_CHUNK_BYTES",
    "NDJSON_MEDIA_TYPE",
    "BlobOutcome",
    "BlobPortabilityStore",
    "BlobRef",
    "ImportCounts",
    "ImportOutcome",
    "ImportReport",
    "PortabilityRecord",
    "PortabilityStore",
    "StreamHeader",
    "add_portability_routes",
    "iter_ndjson_lines",
    "parse_schema",
    "schema_verdict",
    "verified_chunks",
]

log = get_logger("epicurus_core.portability")

NDJSON_MEDIA_TYPE = "application/x-ndjson"
"""Media type of both the export body and the import body — one record per line."""

ImportOutcome = Literal["created", "updated", "skipped"]
"""What one record did to the target: a new row, an overwritten row, or nothing."""

SchemaVerdict = Literal["ok", "older", "newer", "foreign"]
"""How an incoming stream's schema relates to the one the receiving store speaks."""

# A single record line is bounded so a malformed (or hostile) stream cannot be read into
# memory forever while we wait for a newline that never comes. 8 MiB is far above any
# legitimate record — a module that needs more than that per row is modelling a file, and
# files travel in the archive's ``files/`` tree, not in a record's ``data``.
MAX_LINE_BYTES = 8 * 1024 * 1024

BLOB_CHUNK_BYTES = 1024 * 1024
"""How much of a blob a store should hand over per chunk — a hint, not a rule.

Big enough that a gigabyte does not cost a thousand hops through the event loop, small
enough that neither side ever holds a meaningful fraction of an object in memory.
"""


class PortabilityRecord(BaseModel):
    """One exported row: what it is, its stable id, and its payload.

    ``kind`` names the record type *within* the module (``"event"``, ``"task"``, a table
    name — the module's own vocabulary). ``id`` must be **stable across installations**:
    it is the whole basis of the idempotent upsert, so a surrogate autoincrement key is
    exactly the wrong choice. Use the natural id the module already gives the entity.
    """

    kind: str = Field(min_length=1)
    id: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)


class StreamHeader(BaseModel):
    """The first line of an export stream — what produced it.

    ``schema`` is ``"<module>/<n>"``: the module's name and its *record* schema version,
    which it bumps when a record's shape changes in a way an older reader could not handle.
    It is deliberately not the module's release version — that travels beside it in
    ``component_version`` for the manifest and the operator's benefit, and changes far more
    often than the shape does.
    """

    model_config = ConfigDict(populate_by_name=True)

    # ``schema`` shadows a deprecated ``BaseModel`` attribute, which pydantic warns about;
    # the wire name is fixed by the contract, so carry it as an alias instead.
    schema_name: str = Field(validation_alias="schema", serialization_alias="schema", min_length=1)
    component_version: str = ""


class ImportCounts(BaseModel):
    """What happened to one ``kind`` of record during an import."""

    created: int = 0
    updated: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        return self.created + self.updated + self.skipped


class ImportReport(BaseModel):
    """The answer to an import: what landed, per kind, and anything worth saying out loud."""

    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(validation_alias="schema", serialization_alias="schema", default="")
    counts: dict[str, ImportCounts] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    def record(self, kind: str, outcome: ImportOutcome) -> None:
        """Tally one record's outcome under *kind* (creating the bucket on first sight)."""
        counts = self.counts.setdefault(kind, ImportCounts())
        setattr(counts, outcome, getattr(counts, outcome) + 1)

    def warn(self, message: str) -> None:
        """Add a warning, de-duplicated — a per-record complaint must not repeat 10,000 times."""
        if message not in self.warnings:
            self.warnings.append(message)

    @property
    def total(self) -> int:
        """Records seen across every kind."""
        return sum(c.total for c in self.counts.values())


@runtime_checkable
class PortabilityStore(Protocol):
    """What a module implements so :func:`add_portability_routes` can serve it.

    Deliberately tiny — three members, no base class to inherit — so a module can satisfy
    it over whatever storage it already has, and so the helper stays a transport rather
    than a framework.
    """

    @property
    def schema(self) -> str:
        """``"<module>/<n>"`` — the module's name and its record schema version."""
        ...

    def export(self, *, tenant_id: str) -> AsyncIterator[PortabilityRecord]:
        """Stream this tenant's source-of-truth records.

        An async *generator* (called, not awaited). Yield rather than build a list: the
        core writes each line straight into the archive, so a large corpus never has to
        exist in memory on either side.
        """
        ...

    async def import_(
        self,
        *,
        tenant_id: str,
        records: AsyncIterator[PortabilityRecord],
        dry_run: bool,
    ) -> ImportReport:
        """Apply (or, with *dry_run*, only count) an incoming stream.

        Upsert by :attr:`PortabilityRecord.id`; never delete. An unknown ``kind`` is
        counted as ``skipped`` with a warning — a newer source having exported something
        this version does not understand is a fact to report, not an error to fail on.
        """
        ...


class BlobRef(BaseModel):
    """One exported blob: what to ask for, how big it is, and what it is.

    Deliberately carries **no digest**. A store that had to declare one would have to read
    its entire corpus to produce the listing, and read it again to serve the bytes; the core
    hashes the archived member on its way back in, which is the digest that actually protects
    the transfer. ``id`` obeys the same stable-id rule as a record's — and where a module has
    both, using the *same* id for a record and its blob is what pairs them up.
    """

    id: str = Field(min_length=1)
    size: int = 0
    content_type: str = "application/octet-stream"


class BlobOutcome(BaseModel):
    """What one blob did to the target — the byte half of :class:`ImportReport`."""

    id: str = ""
    outcome: ImportOutcome = "skipped"
    # Set when the id already held *different* bytes: they are left exactly as they are and
    # named, never overwritten (the file space's rule, applied to objects).
    warning: str | None = None


@runtime_checkable
class BlobPortabilityStore(Protocol):
    """The optional byte half of the contract, for a module whose data is not only rows.

    Separate from :class:`PortabilityStore` on purpose: the three-member store stays exactly
    as it was for every module that has no bytes, and :func:`add_portability_routes` serves
    the blob routes only for a store that also satisfies this. ``isinstance`` against this
    protocol is the whole of the discovery.
    """

    def blobs(self, *, tenant_id: str) -> AsyncIterator[BlobRef]:
        """Stream a :class:`BlobRef` for every blob this tenant owns (an async generator)."""
        ...

    def open_blob(self, *, tenant_id: str, blob_id: str) -> AsyncIterator[bytes]:
        """Stream one blob's bytes (an async generator).

        Raise :class:`FileNotFoundError` if the id has no bytes — the route turns that into a
        404 *before* it starts a response, so a missing blob is never a truncated download.
        """
        ...

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
        """Write one blob — additively, by content.

        *chunks* arrives already wrapped in :func:`verified_chunks`, so a store never hashes
        the incoming stream itself; it must, however, **consume the whole stream before
        publishing anything**, because that is when a corrupt transfer raises.

        The idempotency rule is by content, not by presence: if the id already holds bytes
        whose digest equals *sha256* the write is ``skipped``, and if it holds *different*
        bytes the incoming ones are discarded and the id is named in
        :attr:`BlobOutcome.warning` as a conflict. Nothing here ever overwrites.
        """
        ...


async def verified_chunks(
    chunks: AsyncIterator[bytes], *, sha256: str, size: int = 0
) -> AsyncIterator[bytes]:
    """Pass *chunks* through, raising ``ValueError`` at end of stream if they do not match.

    The check lands here rather than in each store so that every blob-capable module gets the
    same guarantee from the same code — and it is a *streaming* check, so the bytes are never
    accumulated to be hashed. It fires only once the last chunk has been yielded, which is why
    the contract asks a store to publish nothing until the stream is exhausted: an abort at
    that point is the difference between a refused transfer and a half-written object.

    An empty *sha256* disables the digest check (the size check still runs) — for a caller
    that genuinely cannot know it.
    """
    digest = hashlib.sha256()
    total = 0
    async for chunk in chunks:
        digest.update(chunk)
        total += len(chunk)
        yield chunk
    if size and total != size:
        raise ValueError(f"blob size mismatch: declared {size} bytes, received {total}")
    if sha256 and digest.hexdigest() != sha256:
        raise ValueError(f"blob digest mismatch: declared {sha256}, received {digest.hexdigest()}")


def parse_schema(schema: str) -> tuple[str, int]:
    """Split ``"<module>/<n>"`` into its name and version; raise ``ValueError`` if malformed."""
    name, _, version = schema.rpartition("/")
    if not name or not version.isdigit():
        raise ValueError(f"malformed schema {schema!r}: expected '<module>/<version>'")
    return name, int(version)


def schema_verdict(incoming: str, local: str) -> SchemaVerdict:
    """How *incoming* relates to the *local* schema — the compatibility rule, in one place.

    ``foreign`` (a different module, or an unparseable string) and ``newer`` are refusals;
    ``older`` proceeds with a warning, on the store's promise that it can still read the
    older shape; ``ok`` is the ordinary case. The core's import preview applies exactly
    this rule per component before anything is written, and the module re-applies it at
    its own door so a direct call cannot bypass it.
    """
    try:
        incoming_name, incoming_version = parse_schema(incoming)
        local_name, local_version = parse_schema(local)
    except ValueError:
        return "foreign"
    if incoming_name != local_name:
        return "foreign"
    if incoming_version > local_version:
        return "newer"
    if incoming_version < local_version:
        return "older"
    return "ok"


async def iter_ndjson_lines(chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """Re-assemble complete lines from an arbitrarily chunked byte stream.

    A network read boundary lands wherever it lands — mid-record, mid-multibyte-character
    — so neither ``chunk.split(b"\\n")`` nor decoding per chunk is correct. Buffer, split on
    newlines, decode each *whole* line, and carry the remainder to the next chunk. Blank
    lines are skipped (a trailing newline is not a record).
    """
    buffer = b""
    async for chunk in chunks:
        buffer += chunk
        while b"\n" in buffer:
            raw, _, buffer = buffer.partition(b"\n")
            line = raw.strip()
            if line:
                yield line.decode("utf-8")
        if len(buffer) > MAX_LINE_BYTES:
            raise ValueError(f"record line exceeds {MAX_LINE_BYTES} bytes without a newline")
    tail = buffer.strip()
    if tail:
        yield tail.decode("utf-8")


def _ndjson_line(payload: dict[str, Any]) -> bytes:
    """One NDJSON line: compact JSON, no embedded newlines, terminated."""
    return (json.dumps(payload, separators=(",", ":"), default=str) + "\n").encode("utf-8")


def add_portability_routes(
    app: FastAPI,
    module: EpicurusModule,
    store: PortabilityStore,
) -> None:
    """Serve *store*'s export/import routes on *app* — the module half of #867.

    Takes the ``module`` as well as the store (unlike the sketch in the issue) for the same
    reason :func:`~epicurus_core.module.add_manifest_route` does: the module is where the
    name and version live, and reading them from it keeps the header line honest without
    asking every store to restate what its module already knows.

    Registering these routes does **not** raise the manifest flag — declare
    ``EpicurusModule(portable=True)`` too, or the core will never call them. They are kept
    separate on purpose: the flag is what the core fans out over, and a module that serves
    the routes while still wiring up its store should be able to say "not yet".
    """

    def _tenant(tenant_id: str | None) -> str:
        if not tenant_id:
            raise HTTPException(status_code=400, detail="tenant_id is required")
        try:
            return validate_tenant_id(tenant_id)
        except TenantError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/export")
    async def export_records(
        tenant_id: str | None = Query(default=None, description="Tenant to export"),
    ) -> StreamingResponse:
        """Stream this tenant's source-of-truth records as NDJSON (header line first)."""
        tenant = _tenant(tenant_id)

        async def body() -> AsyncIterator[bytes]:
            header = StreamHeader(
                schema_name=store.schema,
                component_version=(await module.manifest()).version,
            )
            yield _ndjson_line(header.model_dump(by_alias=True))
            count = 0
            async for record in store.export(tenant_id=tenant):
                count += 1
                yield _ndjson_line(record.model_dump())
            log.info(
                "portability export served",
                module=module.name,
                tenant=tenant,
                records=count,
            )

        return StreamingResponse(body(), media_type=NDJSON_MEDIA_TYPE)

    @app.post("/import", response_model=ImportReport)
    async def import_records(
        request: Request,
        tenant_id: str | None = Query(default=None, description="Tenant to import into"),
        dry_run: bool = Query(default=False, description="Count only; write nothing"),
    ) -> ImportReport:
        """Apply an NDJSON stream to this tenant — additively, upserting by stable id."""
        tenant = _tenant(tenant_id)
        lines = iter_ndjson_lines(request.stream())
        # The header is consumed here, not by the store: compatibility is a contract
        # question, and answering it before the first record reaches the store is what makes
        # a refusal clean rather than half-applied.
        header = await _read_header(lines)
        verdict = schema_verdict(header.schema_name, store.schema)
        if verdict in ("foreign", "newer"):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"incompatible stream: {header.schema_name!r} cannot be imported into "
                    f"{store.schema!r} ({verdict})"
                ),
            )
        try:
            report = await store.import_(
                tenant_id=tenant,
                records=_parse_records(lines),
                dry_run=dry_run,
            )
        except ValueError as exc:  # a malformed line — the caller's stream, not our bug
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        report.schema_name = store.schema
        if verdict == "older":
            report.warn(
                f"archive was written by an older schema ({header.schema_name}); "
                f"imported into {store.schema}"
            )
        log.info(
            "portability import applied",
            module=module.name,
            tenant=tenant,
            dry_run=dry_run,
            records=report.total,
        )
        return report

    if isinstance(store, BlobPortabilityStore):
        _add_blob_routes(app, module, store, _tenant)


def _add_blob_routes(
    app: FastAPI,
    module: EpicurusModule,
    store: BlobPortabilityStore,
    tenant_of: Callable[[str | None], str],
) -> None:
    """Serve the three blob routes for a store that has bytes as well as rows (#876).

    Split out of :func:`add_portability_routes` only for size; it is not separately callable
    on purpose — a module that served these without the record routes would be a module the
    core's fan-out never reaches, since the fan-out is driven by the NDJSON member.
    """

    @app.get("/export/blobs")
    async def export_blobs(
        tenant_id: str | None = Query(default=None, description="Tenant to export"),
    ) -> StreamingResponse:
        """List this tenant's blobs as NDJSON — one :class:`BlobRef` per line, no header.

        A module without bytes never registers this route, so the core reads a 404 here as
        "this module has no blob half" rather than as a failure.
        """
        tenant = tenant_of(tenant_id)

        async def body() -> AsyncIterator[bytes]:
            count = 0
            async for ref in store.blobs(tenant_id=tenant):
                count += 1
                yield _ndjson_line(ref.model_dump())
            log.info(
                "portability blob listing served",
                module=module.name,
                tenant=tenant,
                blobs=count,
            )

        return StreamingResponse(body(), media_type=NDJSON_MEDIA_TYPE)

    @app.get("/export/blobs/{blob_id:path}")
    async def export_blob(
        blob_id: str = Path(description="The blob's stable id"),
        tenant_id: str | None = Query(default=None, description="Tenant to export"),
    ) -> StreamingResponse:
        """Stream one blob's bytes. 404 if the id has none — decided before the first byte."""
        tenant = tenant_of(tenant_id)
        chunks = store.open_blob(tenant_id=tenant, blob_id=blob_id)
        # Pull the first chunk here, inside the handler, so a missing blob is an honest 404
        # instead of a 200 that dies two bytes in — a StreamingResponse cannot take back its
        # status line once it has been sent.
        try:
            first = await anext(chunks)
        except StopAsyncIteration:
            first = b""  # a legitimately empty blob
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"no such blob: {blob_id}") from exc

        async def body() -> AsyncIterator[bytes]:
            if first:
                yield first
            async for chunk in chunks:
                yield chunk

        return StreamingResponse(body(), media_type="application/octet-stream")

    @app.put("/import/blobs/{blob_id:path}", response_model=BlobOutcome)
    async def import_blob(
        request: Request,
        blob_id: str = Path(description="The blob's stable id"),
        tenant_id: str | None = Query(default=None, description="Tenant to import into"),
        sha256: str = Query(default="", description="Expected SHA-256 of the body"),
        size: int = Query(default=0, description="Expected byte length of the body"),
        content_type: str = Query(
            default="application/octet-stream", description="Media type to store the blob with"
        ),
    ) -> BlobOutcome:
        """Write one blob into this tenant — additively, by content (never an overwrite)."""
        tenant = tenant_of(tenant_id)
        try:
            outcome = await store.put_blob(
                tenant_id=tenant,
                blob_id=blob_id,
                sha256=sha256,
                size=size,
                content_type=content_type,
                chunks=verified_chunks(request.stream(), sha256=sha256, size=size),
            )
        except ValueError as exc:  # a corrupt or truncated body — the caller's, not ours
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        outcome.id = outcome.id or blob_id
        log.info(
            "portability blob imported",
            module=module.name,
            tenant=tenant,
            blob=blob_id,
            outcome=outcome.outcome,
        )
        return outcome


async def _read_header(lines: AsyncIterator[str]) -> StreamHeader:
    """Consume and validate the stream's first line."""
    async for line in lines:
        try:
            return StreamHeader.model_validate_json(line)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"malformed stream header: {exc}") from exc
    raise HTTPException(status_code=400, detail="empty stream: no header line")


async def _parse_records(lines: AsyncIterator[str]) -> AsyncIterator[PortabilityRecord]:
    """Validate each remaining line into a record, naming the line that broke."""
    number = 1  # the header was line 0
    async for line in lines:
        number += 1
        try:
            yield PortabilityRecord.model_validate_json(line)
        except Exception as exc:
            raise ValueError(f"malformed record on line {number}: {exc}") from exc
