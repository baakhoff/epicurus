"""Entry point: ``python -m epicurus_tasks``."""

import uvicorn

from epicurus_tasks.app import app

uvicorn.run(app, host="0.0.0.0", port=8080)
