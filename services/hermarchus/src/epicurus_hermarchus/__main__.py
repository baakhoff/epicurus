"""Run Hermarchus with uvicorn (the container entrypoint)."""

from __future__ import annotations

import sys

import uvicorn

from epicurus_hermarchus.app import create_app
from epicurus_hermarchus.settings import HermarchusSettings, SettingsError


def main() -> None:
    try:
        settings = HermarchusSettings.load()
    except SettingsError as exc:
        # Refuse to start rather than serve an open door (no token, a bad profile, a
        # workspace that cannot be used). The message is the whole remedy.
        print(f"hermarchus: refusing to start: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    # Bind all interfaces — the process runs inside its own container; the published port
    # is loopback-bound by the Compose overlay, and a ClusterIP Service on Kubernetes.
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
