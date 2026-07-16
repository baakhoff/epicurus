#!/usr/bin/env python3
"""Core image entrypoint — provision the tenant file-space root as root, then drop to the app user.

The core is the sole writer of the shared file space (`/data`, ADR-0063). A fresh
`epicurus-files` named volume is created **root-owned**, and the app runs as uid 10001 — which
cannot `chown` a root-owned volume itself. The old `files-init` one-shot did that chown; this
entrypoint folds it into the core image (#421, ADR-0069): start as root, create and `chown`
**only** the tenant root, drop privileges, then `exec` the app as uid 10001.

The chown is **surgical** — the tenant root directory only, never recursive. An operator may
bind-mount an existing tree (e.g. an Obsidian vault) at the file-space path; its contents must
be left untouched, so we touch only `<FILES_ROOT>/<DEFAULT_TENANT_ID>` and never `-R` into it.
The module subtrees (`knowledge/`, `notes/`, …) are created by the core on first write — readers
already tolerate a missing one — so they are deliberately not pre-created here.

Optionally joins the host's Docker-socket group (`DOCKER_GID`, #622, ADR-0099): the opt-in
`compose.docker-socket.yaml` overlay bind-mounts `/var/run/docker.sock`, but the mount alone
does not make it *reachable* — the socket is host-owned (typically `root:docker`, mode 660),
and dropping to uid 10001 leaves the app in no group the image defines (there is no `docker`
group in this image; nothing to `initgroups()` into). `DOCKER_GID` names the *host's* group so
we can join it explicitly, while still privileged enough to do so.
"""

from __future__ import annotations

import os
import pwd
import sys

# The app runs as this uid (the `epicurus` user created in the Dockerfile).
APP_UID = 10001


def _provision_tenant_root(uid: int, gid: int) -> None:
    """Create + own only the tenant file-space root so the app can write under it."""
    files_root = os.environ.get("FILES_ROOT", "/data")
    tenant = os.environ.get("DEFAULT_TENANT_ID", "local")
    tenant_root = os.path.join(files_root, tenant)
    # Non-recursive on purpose: own the tenant dir, never its (possibly bind-mounted) contents.
    os.makedirs(tenant_root, exist_ok=True)
    os.chown(tenant_root, uid, gid)
    os.chmod(tenant_root, 0o755)


def _drop_privileges(pw: pwd.struct_passwd) -> None:
    """Drop from root to *pw* — supplementary groups, gid, uid, then its environment.

    Resetting HOME/USER/LOGNAME matters: we inherited root's environment, and a tool that
    resolves a path under ``$HOME`` (e.g. asyncpg's default ``~/.postgresql/postgresql.key``
    existence check during connect) would otherwise stat ``/root`` and raise ``PermissionError``
    as the unprivileged uid. The old ``USER epicurus`` set HOME via the passwd entry; we do too.

    ``initgroups`` only grants groups *this image's* ``/etc/group`` defines for ``epicurus``
    (none) — it cannot know a host's ``docker`` group GID, which varies per machine. When the
    operator has opted into Docker-socket access (#622, ADR-0099) and set ``DOCKER_GID``, join
    it explicitly here, while still privileged enough to (``setgroups`` needs the same
    capability ``initgroups`` used, so this must happen before ``setuid`` drops it).
    """
    os.initgroups(pw.pw_name, pw.pw_gid)
    docker_gid = os.environ.get("DOCKER_GID")
    if docker_gid:
        os.setgroups([*os.getgroups(), int(docker_gid)])
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    os.environ["HOME"] = pw.pw_dir
    os.environ["USER"] = pw.pw_name
    os.environ["LOGNAME"] = pw.pw_name


def main() -> None:
    # The chown only matters as root (the volume is root-owned on first boot). If we are already
    # unprivileged — a dev run, or a platform that injects a non-root user — skip it and just exec.
    if os.geteuid() == 0:
        pw = pwd.getpwuid(APP_UID)
        _provision_tenant_root(pw.pw_uid, pw.pw_gid)
        _drop_privileges(pw)
    # Exec the CMD (defaults to the app) so the entrypoint process is replaced and stays PID 1,
    # forwarding signals to the app.
    args = sys.argv[1:] or ["python", "-m", "epicurus_core_app"]
    os.execvp(args[0], args)


if __name__ == "__main__":
    main()
