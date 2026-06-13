"""Entry point: python -m epicurus_mail"""

import uvicorn

uvicorn.run("epicurus_mail.app:app", host="0.0.0.0", port=8080, log_config=None)
