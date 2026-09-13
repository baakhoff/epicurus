"""The backup scripts must name volumes that actually exist (#895).

``infra/backups/backup.sh`` addresses Docker named volumes by
``${COMPOSE_PROJECT_NAME}_<name>`` and skips any that is missing — quietly, with a
log line and an exit code of 0. So a renamed or retired volume does not fail the
backup, it *shrinks* it, and the operator finds out at restore time. That is not a
hypothetical: the loop carried ``knowledge-vault-data`` and ``storage-root-data``
for months after the file space moved to ``epicurus-files``, and it never carried
``epicurus-files`` — the core's ``/data``, the source of truth behind knowledge,
notes and storage — at all.

So this test asks three things of the scripts, from the compose files themselves:

* every name they address is a volume the stack actually declares;
* backup and restore address the *same* set (an archive nothing restores is as
  useless as a restore with no archive);
* every volume the data plane declares is either backed up or **named in
  backup.sh's own list of deliberate exclusions** — so adding a stateful volume
  forces a decision here instead of silently falling out of the backup.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKUP = REPO / "infra" / "backups" / "backup.sh"
RESTORE = REPO / "infra" / "backups" / "restore.sh"
DATA_PLANE = REPO / "infra" / "compose" / "docker-compose.yml"

# The shared file space is not in the VOLUMES array: where it lives depends on
# EPICURUS_FILES_ROOT (a named volume by default, a host path on a deployment that
# points at one), so both scripts branch on it. Its archive is always this name.
FILES_ARCHIVE = "epicurus-files"


def _compose_files() -> list[Path]:
    """Every compose file in the repo, excluding the cookiecutter template."""
    found: list[Path] = []
    for pattern in ("compose*.yaml", "compose*.yml", "docker-compose*.yml", "docker-compose*.yaml"):
        found.extend(
            p
            for p in REPO.rglob(pattern)
            if "node_modules" not in p.parts and "{{cookiecutter.service_slug}}" not in p.parts
        )
    assert found, "no compose files found — the glob is wrong"
    return sorted(set(found))


def _declared_volumes(path: Path) -> set[str]:
    """The top-level ``volumes:`` keys declared in one compose file."""
    names: set[str] = set()
    in_block = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(r"^volumes:\s*$", line):
            in_block = True
            continue
        if in_block:
            if line and not line[0].isspace():
                in_block = False
                continue
            match = re.match(r"^  ([A-Za-z0-9_.-]+):", line)
            if match:
                names.add(match.group(1))
    return names


def _volumes_array(path: Path) -> list[str]:
    """The ``VOLUMES=(…)`` array a backup script loops over."""
    match = re.search(r"^VOLUMES=\(([^)]*)\)", path.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{path.name} no longer declares a VOLUMES=(…) array — update this test"
    return match.group(1).split()


def test_every_backed_up_volume_is_one_the_stack_declares() -> None:
    declared: set[str] = set()
    for compose in _compose_files():
        declared |= _declared_volumes(compose)
    assert FILES_ARCHIVE in declared, (
        "the compose files no longer declare epicurus-files — update this test and both scripts"
    )
    for script in (BACKUP, RESTORE):
        unknown = [v for v in _volumes_array(script) if v not in declared]
        assert not unknown, (
            f"{script.name} addresses {unknown}, which no compose file declares. A missing "
            "volume is SKIPPED, not an error, so this backup would exit 0 having archived less "
            "than it says."
        )


def test_backup_and_restore_address_the_same_volumes() -> None:
    assert _volumes_array(BACKUP) == _volumes_array(RESTORE), (
        "backup.sh and restore.sh disagree about which volumes a backup contains — one of "
        "them is archiving something nothing restores, or restoring something nothing archives"
    )


def test_every_data_plane_volume_is_either_backed_up_or_deliberately_excluded() -> None:
    """A new stateful volume must be decided about, not silently left out."""
    text = BACKUP.read_text(encoding="utf-8")
    covered = set(_volumes_array(BACKUP)) | {FILES_ARCHIVE}
    undecided = [
        name
        for name in sorted(_declared_volumes(DATA_PLANE))
        if name not in covered and name not in text
    ]
    assert not undecided, (
        f"{undecided} are declared by the data plane but neither backed up nor named in "
        "backup.sh's list of deliberate exclusions. Decide: archive it, or say in that "
        "comment why a restore does not need it."
    )


def test_both_scripts_handle_the_file_space_in_either_shape() -> None:
    """EPICURUS_FILES_ROOT may be a host path — then there is no volume to snapshot.

    The owner's own box points it at ``/srv/epicurus-files``; a script that only knew
    about the named volume would back up an empty default volume and report success.
    """
    for script in (BACKUP, RESTORE):
        text = script.read_text(encoding="utf-8")
        assert "EPICURUS_FILES_ROOT" in text, (
            f"{script.name} ignores EPICURUS_FILES_ROOT: on a deployment that bind-mounts the "
            "file space it would address a named volume that holds nothing"
        )
        assert '"${FILES_ROOT}" == */*' in text, (
            f"{script.name} does not branch on the host-path shape of EPICURUS_FILES_ROOT "
            "(compose's own rule: a value containing a '/' is a bind mount)"
        )
        assert FILES_ARCHIVE in text, f"{script.name} never mentions the {FILES_ARCHIVE} archive"


def test_the_file_space_archive_name_is_the_same_on_both_branches() -> None:
    """One archive name, whichever shape the file space has.

    A deployment moving from the default named volume to a bind mount (or back) is
    ordinary, and it is exactly the moment a restore matters most — so the archive
    cannot be named after the shape it was taken from. Both branches of both scripts
    therefore pass ``epicurus-files`` as the explicit archive name.
    """
    for script in (BACKUP, RESTORE):
        text = script.read_text(encoding="utf-8")
        branches = re.findall(rf'^\s*\w+ "\$\{{[^"]+\}}" {FILES_ARCHIVE}$', text, re.M)
        assert len(branches) == 2, (
            f"{script.name} has {len(branches)} file-space call(s) naming the "
            f"{FILES_ARCHIVE} archive explicitly; expected one per shape (volume and host path)"
        )
