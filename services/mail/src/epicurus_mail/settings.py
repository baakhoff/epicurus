"""Mail-service configuration — CoreSettings plus the platform URL."""

from __future__ import annotations

from epicurus_core import CoreSettings


class MailSettings(CoreSettings):
    """Adds the platform API endpoint to the shared settings."""

    # Core service base URL (platform API). On the Docker network: http://core-app:8080.
    platform_url: str = "http://localhost:8080"
