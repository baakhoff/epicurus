"""Run Hermarchus with uvicorn (the container entrypoint)."""

from __future__ import annotations

import os
import sys

import uvicorn

from epicurus_hermarchus.app import create_app
from epicurus_hermarchus.sandbox import harden_process
from epicurus_hermarchus.settings import HermarchusSettings, SettingsError

# How long open connections (following event streams) get to end on SIGTERM.
STREAM_SHUTDOWN_GRACE_S = 5


def main() -> None:
    try:
        settings = HermarchusSettings.load()
    except SettingsError as exc:
        # Refuse to start rather than serve an open door (no token, a bad profile, a
        # workspace that cannot be used). The message is the whole remedy.
        print(f"hermarchus: refusing to start: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    # Before any agent can start: an agent runs as this uid and could otherwise read the
    # token out of /proc/<this pid>/environ. The token is held in `settings` from here on.
    if not harden_process():
        print(
            "hermarchus: warning: could not make the process non-dumpable; agents running as "
            "this user can read its environment (and the bearer token) through /proc",
            file=sys.stderr,
        )
    os.environ.pop("HERMARCHUS_TOKEN", None)
    # Bind all interfaces — the process runs inside its own container; the published port
    # is loopback-bound by the Compose overlay, and a ClusterIP Service on Kubernetes.
    #
    # `timeout_graceful_shutdown`: a controller normally holds a following event stream open,
    # and uvicorn otherwise waits for every connection to close before the lifespan shutdown
    # runs — so on SIGTERM no agent would be stopped and no session marked interrupted before
    # the container's grace ran out. After this many seconds open streams are cut, then the
    # lifespan stops every agent.
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.port,
        timeout_graceful_shutdown=STREAM_SHUTDOWN_GRACE_S,
    )


if __name__ == "__main__":
    main()
