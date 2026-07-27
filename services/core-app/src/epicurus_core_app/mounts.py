"""Operator-declared external file mounts (#731): additional roots beside the tenant space.

core-app runs in a container, so an arbitrary host path cannot be attached at runtime — a
bind mount is a compose change plus a container recreate (`services/core-app/compose.
external-mounts.yaml`, opt-in, never auto-loaded — see docs/infrastructure/index.md). This
module parses the resulting `FILES_EXTERNAL_MOUNTS*` settings into validated :class:`MountSpec`
rows and builds one :class:`~epicurus_core.files.LocalFileStore` per mount — the simplest shape
that satisfies constraint #3 (no new backend abstraction) while keeping each mount's content
fully isolated from the tenant space and from every other mount.

Addressing (used everywhere — the platform API's `path` query param, `PlatformClient.files_*`,
and the agent's `storage_*` tools): a mount-relative path is `mount:<name>/<sub-path>`; an empty
sub-path (`mount:<name>`) addresses the mount's own root. `files_routes.py` resolves this prefix
before any tenant-store call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from epicurus_core.files import FileStore, LocalFileStore

MOUNT_PREFIX = "mount:"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_MODES = {"ro", "rw"}


@dataclass(frozen=True)
class MountSpec:
    """One operator-declared mount, parsed and validated from settings — no I/O yet."""

    name: str
    path: Path
    read_only: bool
    indexed: bool
    exclude: tuple[str, ...]


@dataclass(frozen=True)
class Mount:
    """A :class:`MountSpec` paired with the :class:`FileStore` instance that serves it."""

    spec: MountSpec
    store: FileStore

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def read_only(self) -> bool:
        return self.spec.read_only

    @property
    def indexed(self) -> bool:
        return self.spec.indexed

    @property
    def exclude(self) -> tuple[str, ...]:
        return self.spec.exclude


def _parse_exclude(raw: str) -> dict[str, tuple[str, ...]]:
    """Parse ``name=pat1|pat2;name2=pat3`` into ``{name: (pat1, pat2)}``."""
    out: dict[str, tuple[str, ...]] = {}
    for block in raw.split(";"):
        block = block.strip()
        if not block:
            continue
        name, sep, patterns = block.partition("=")
        if not sep:
            raise ValueError(
                f"malformed files_external_mounts_exclude entry (want name=pat1|pat2): {block!r}"
            )
        out[name.strip()] = tuple(p.strip() for p in patterns.split("|") if p.strip())
    return out


def parse_mount_specs(*, mounts: str, indexed: str = "", exclude: str = "") -> list[MountSpec]:
    """Parse the ``files_external_mounts*`` settings into validated :class:`MountSpec` rows.

    ``mounts`` is comma-separated ``name:container-path[:ro|rw]`` (mode defaults to ``ro``);
    ``indexed`` is a comma-separated list of mount names opted into indexing/watching (default:
    none — a whole-drive mount must never auto-index); ``exclude`` scopes glob patterns per
    mount name (see :func:`_parse_exclude`). Raises ``ValueError`` — on a malformed entry, a
    duplicate name, an invalid mode, or an ``indexed``/``exclude`` reference to a name not
    declared in ``mounts`` — so a typo fails startup loudly instead of silently granting (or
    silently not indexing) the wrong drive.
    """
    indexed_names = {n.strip() for n in indexed.split(",") if n.strip()}
    exclude_by_name = _parse_exclude(exclude)

    specs: list[MountSpec] = []
    seen: set[str] = set()
    for raw_entry in mounts.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) == 2:
            name, path_str, mode = *parts, "ro"
        elif len(parts) == 3:
            name, path_str, mode = parts
        else:
            raise ValueError(
                f"malformed files_external_mounts entry (want name:path[:ro|rw]): {entry!r}"
            )
        name = name.strip()
        path_str = path_str.strip()
        mode = mode.strip().lower()
        if not _NAME_RE.match(name):
            raise ValueError(
                f"invalid mount name {name!r}: 1-63 chars, lowercase alphanumeric plus '-'/'_', "
                "starting with a letter or digit"
            )
        if name in seen:
            raise ValueError(f"duplicate mount name: {name!r}")
        if mode not in _MODES:
            raise ValueError(f"invalid mode for mount {name!r}: {mode!r} (want 'ro' or 'rw')")
        if not path_str:
            raise ValueError(f"mount {name!r} has an empty path")
        seen.add(name)
        specs.append(
            MountSpec(
                name=name,
                path=Path(path_str),
                read_only=(mode == "ro"),
                indexed=name in indexed_names,
                exclude=exclude_by_name.get(name, ()),
            )
        )

    unknown_indexed = indexed_names - seen
    if unknown_indexed:
        raise ValueError(
            "files_external_mounts_indexed names a mount not declared in "
            f"files_external_mounts: {sorted(unknown_indexed)}"
        )
    unknown_exclude = set(exclude_by_name) - seen
    if unknown_exclude:
        raise ValueError(
            "files_external_mounts_exclude names a mount not declared in "
            f"files_external_mounts: {sorted(unknown_exclude)}"
        )
    return specs


def build_mounts(specs: list[MountSpec]) -> dict[str, Mount]:
    """Construct one local-FS store per declared mount, keyed by name.

    ``tenant_subdir=False``: the mount root already names one directory (#731) — nesting a
    tenant segment under it would require the operator to create that subfolder on their own
    drive. Local-FS only: an external mount is host-filesystem content by definition, so there
    is no S3 equivalent of "bind a host directory" to swap in.
    """
    return {
        spec.name: Mount(spec=spec, store=LocalFileStore(spec.path, tenant_subdir=False))
        for spec in specs
    }
