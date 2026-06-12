"""Entry point: ``python -m epicurus_knowledge``."""

import uvicorn

from epicurus_knowledge.app import app

uvicorn.run(app, host="0.0.0.0", port=8080)
