"""Run the echo service with uvicorn (the container entrypoint)."""

from __future__ import annotations

import uvicorn

from epicurus_echo.app import app


def main() -> None:
    # Bind all interfaces — the process runs inside its own container.
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
