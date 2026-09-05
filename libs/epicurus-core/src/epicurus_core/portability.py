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

import json
from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, runtime_checkable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from epicurus_core.logging import get_logger
from epicurus_core.module import EpicurusModule
from epicurus_core.tenancy import TenantError, validate_tenant_id

__all__ = [
    "NDJSON_MEDIA_TYPE",
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
