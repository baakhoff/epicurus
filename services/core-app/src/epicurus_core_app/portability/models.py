"""The wire shapes of tenant portability — the archive manifest and the two job views (#867).

One vocabulary, used three times over: the export job reports **progress** as a list of
:class:`ComponentEntry`, the archive's ``manifest.json`` records the *same* entries as its
inventory of what was written, and the import preview grades each of those entries against
what this installation can accept. Keeping one shape across all three is what lets the
Settings card render export progress and import preview with the same table.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ARCHIVE_MANIFEST_MEMBER",
    "CORE_MEMBER_PREFIX",
    "FILES_MEMBER_PREFIX",
    "MODULE_MEMBER_PREFIX",
    "PORTABILITY_FORMAT_VERSION",
    "ArchiveManifest",
    "ComponentEntry",
    "ComponentKind",
    "ComponentState",
    "ExclusionEntry",
    "ExportJobView",
    "FileTransfer",
    "ImportComponentPreview",
    "ImportComponentResult",
    "ImportJobView",
    "ImportPreview",
    "ImportReportView",
    "ImportVerdict",
    "PortabilityJobSummary",
    "SecretsInventory",
    "as_json",
]

PORTABILITY_FORMAT_VERSION = 1
"""The archive layout's own version.

Bumped only when the *container* changes — the member names, the manifest's shape, the
record envelope — never when a component's data changes (each component carries its own
``schema``). A mismatch here is refused at preview: a reader that does not know the layout
cannot safely half-read it, and there is nothing useful to salvage from guessing.
"""

ARCHIVE_MANIFEST_MEMBER = "manifest.json"
CORE_MEMBER_PREFIX = "core/"
MODULE_MEMBER_PREFIX = "modules/"
FILES_MEMBER_PREFIX = "files/"

ComponentKind = Literal["core", "module", "files"]
"""Which half of the archive a component belongs to."""

ComponentState = Literal["pending", "running", "included", "skipped", "failed"]
"""Where a component got to.

``skipped`` is a *fact*, not a failure: a module that is disabled, unreachable, or simply
not portable is recorded with its reason and the job carries on (the whole point of a
best-effort fan-out). ``failed`` is reserved for a component that was supposed to work and
did not — it is reported, and still does not abort the job.
"""


class ComponentEntry(BaseModel):
    """One component of an archive: what it is, how it went, and where it landed."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    kind: ComponentKind
    state: ComponentState = "pending"
    # Records written (core sets, module streams) or files copied (the file space).
    count: int = 0
    # ``"<module>/<n>"`` for a module, ``"core/<n>"`` for a core set; absent for files.
    schema_name: str | None = Field(
        default=None, validation_alias="schema", serialization_alias="schema"
    )
    # The source component's release version — a module's manifest version, or core-app's.
    version: str | None = None
    # The archive member this component was written to (``modules/calendar.ndjson``, …).
    member: str | None = None
    # Why a component was skipped, in the operator's words ("module unreachable").
    reason: str | None = None
    error: str | None = None


class ExclusionEntry(BaseModel):
    """Something deliberately left out of the archive, and why.

    Recorded in the manifest rather than merely documented: an operator restoring into a
    fresh install needs to know that the mail cache and every Qdrant collection are *meant*
    to be missing, and that the import rebuilds them.
    """

    component: str
    reason: str


class SecretsInventory(BaseModel):
    """What the source had that the archive deliberately does not carry (#867).

    Provider API keys and OAuth tokens live in OpenBao and never enter the archive, so the
    only useful thing the export can do is *name* them: these provider aliases had a key,
    these accounts were connected. The import report replays the list as "re-enter these,
    reconnect those" instead of leaving the operator to discover it one broken feature at
    a time.
    """

    provider_keys: list[str] = Field(default_factory=list)
    connected_accounts: list[str] = Field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.provider_keys and not self.connected_accounts


class ArchiveManifest(BaseModel):
    """``manifest.json`` — the archive's self-description, read before anything is applied."""

    format_version: int = PORTABILITY_FORMAT_VERSION
    tenant: str
    created_at: str
    core_app_version: str
    epicurus_core_version: str
    components: list[ComponentEntry] = Field(default_factory=list)
    exclusions: list[ExclusionEntry] = Field(default_factory=list)
    secrets: SecretsInventory = Field(default_factory=SecretsInventory)


class PortabilityJobSummary(BaseModel):
    """One row of ``GET /platform/v1/portability/jobs`` — enough to re-attach, no more (#877).

    Deliberately not either job view: the list exists so a reloaded tab can find the job it
    started, and dragging every component entry, manifest and report along for twenty rows
    would make the cheapest read on the surface the most expensive one. The card follows an
    id it recognises here into the full view it already had.
    """

    id: str
    kind: Literal["export", "import"]
    # A plain string rather than a Literal: the two kinds have different vocabularies
    # (``running``/``ready``/``failed`` against ``staged``/``running``/``done``/``failed``),
    # and a union of both would let this model claim a pairing neither job can produce.
    status: str
    created_at: str
    updated_at: str
    # Only an export has an archive, only once it is ``ready``, and only for as long as its
    # staged file survives (staging is a disposable cache). Answered from the filesystem, so
    # the card offers a download link exactly when the download would work.
    archive_available: bool = False
    size_bytes: int = 0


class ExportJobView(BaseModel):
    """``GET /platform/v1/portability/exports/{id}``."""

    id: str
    status: Literal["running", "ready", "failed"]
    created_at: str
    updated_at: str
    progress: list[ComponentEntry] = Field(default_factory=list)
    manifest: ArchiveManifest | None = None
    size_bytes: int = 0
    error: str | None = None


ImportVerdict = Literal["ok", "warning", "refused"]
"""Whether a component in an uploaded archive can be applied here.

``warning`` is the older-schema case: applicable, but the operator should know the source
was behind. ``refused`` isolates one component (a newer schema, an unknown module) without
touching the rest — a single incompatible module must not cost the operator their
conversations.
"""


class ImportComponentPreview(BaseModel):
    """One component of an uploaded archive, graded against this installation."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    kind: ComponentKind
    records: int = 0
    verdict: ImportVerdict = "ok"
    detail: str | None = None
    schema_name: str | None = Field(
        default=None, validation_alias="schema", serialization_alias="schema"
    )


class ImportPreview(BaseModel):
    """What ``POST /platform/v1/portability/imports`` answers: read the archive, apply nothing."""

    manifest: ArchiveManifest
    components: list[ImportComponentPreview] = Field(default_factory=list)
    # False when the *whole* archive is unusable here (a format-version mismatch). Individual
    # refused components do not clear this — the rest still apply.
    compatible: bool = True
    refusal: str | None = None


class ImportComponentResult(BaseModel):
    """What one component actually did on apply."""

    name: str
    kind: ComponentKind
    state: ComponentState
    created: int = 0
    updated: int = 0
    skipped: int = 0
    warnings: list[str] = Field(default_factory=list)
    reason: str | None = None
    error: str | None = None


class FileTransfer(BaseModel):
    """The file space's half of an import: written, already-identical, and differing."""

    written: int = 0
    skipped: int = 0
    # Files present on both sides with different bytes. Never overwritten — import is
    # additive — so each one is named for the operator to reconcile by hand.
    conflicts: list[str] = Field(default_factory=list)


class ImportReportView(BaseModel):
    """The final answer of an apply: per component, plus the two rebuilds it triggered."""

    components: list[ImportComponentResult] = Field(default_factory=list)
    files: FileTransfer = Field(default_factory=FileTransfer)
    # The forced file rescan that re-derives ``core_files`` from the imported tree, and the
    # re-embed fan-out that rebuilds every module's vectors (#332). Reported, not assumed:
    # either can fail without invalidating the data that already landed.
    rescan_entries: int | None = None
    rescan_error: str | None = None
    # Whether that rescan ran with the #848 mass de-index fuse bypassed. It always does after
    # an import — a fresh install's empty index flipping to a populated one is precisely what
    # the fuse refuses — and saying so in the report is what keeps that from being a silent
    # override of a safety rule the operator never saw waived.
    rescan_forced: bool = False
    reembed: list[dict[str, str]] = Field(default_factory=list)
    reembed_error: str | None = None
    # Repeated from the source manifest so the operator sees it at the moment it matters.
    reenter_secrets: SecretsInventory = Field(default_factory=SecretsInventory)


class ImportJobView(BaseModel):
    """``GET /platform/v1/portability/imports/{id}``."""

    id: str
    status: Literal["staged", "running", "done", "failed"]
    created_at: str
    updated_at: str
    preview: ImportPreview | None = None
    report: ImportReportView | None = None
    error: str | None = None


def as_json(model: BaseModel) -> dict[str, Any]:
    """A JSON-safe mapping for a persisted job column (aliases applied, like the wire)."""
    return model.model_dump(by_alias=True, mode="json")
