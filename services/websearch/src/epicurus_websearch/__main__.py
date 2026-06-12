"""Entry point: ``python -m epicurus_websearch``."""

import uvicorn

from epicurus_websearch.app import app

uvicorn.run(app, host="0.0.0.0", port=8080)
