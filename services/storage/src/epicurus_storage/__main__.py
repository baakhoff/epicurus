"""Entry point: ``python -m epicurus_storage``."""

import uvicorn

from epicurus_storage.app import app

uvicorn.run(app, host="0.0.0.0", port=8080)
