"""Run the {{ cookiecutter.service_name }} service with uvicorn (container entrypoint)."""

from __future__ import annotations

import uvicorn

from {{ cookiecutter.package_name }}.app import app


def main() -> None:
    # Bind all interfaces — the process runs inside its own container.
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
