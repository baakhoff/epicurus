"""Entry point: ``python -m epicurus_calendar``."""

import uvicorn

from epicurus_calendar.app import app

uvicorn.run(app, host="0.0.0.0", port=8080)
