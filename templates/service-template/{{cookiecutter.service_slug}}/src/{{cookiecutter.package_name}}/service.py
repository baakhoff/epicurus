"""The {{ cookiecutter.service_name }} module: its tools and declared events.

Built on `epicurus-core`. Replace the sample `ping` tool with the module's real
capability, and declare any NATS events it emits/consumes with `module.emits(...)`
/ `module.consumes(...)`.
"""

from __future__ import annotations

from epicurus_core import EpicurusModule

MODULE_NAME = "{{ cookiecutter.service_slug }}"


def build_module() -> EpicurusModule:
    """Build the {{ cookiecutter.service_name }} module and register its tools/events."""
    module = EpicurusModule(
        MODULE_NAME,
        version="0.1.0",
        description="{{ cookiecutter.description }}",
    )

    @module.tool()
    def ping(message: str = "hello") -> str:
        """A sample tool — replace with the module's real capability."""
        return f"{MODULE_NAME}: {message}"

    return module


# ── Reference patterns ────────────────────────────────────────────────────────
# Two things parallel modules have repeatedly hand-rolled wrong. Copy these shapes
# instead of reinventing them (see the pitfalls list in .workspace/AGENTS.md).
#
# 1. Calling a third-party API on the user's behalf (Google, …)? A module never
#    holds a client secret or refresh token. Ask the core for a ready, auto-
#    refreshed access token — ALWAYS via PlatformClient.get_oauth_token, never a
#    bespoke HTTP call to /platform/v1/oauth/... and never your own token method
#    (one contract, owned by the core — ADR-0016):
#
#        from epicurus_core import CoreSettings, PlatformClient
#
#        settings = CoreSettings()
#        platform = PlatformClient(
#            base_url=settings.platform_url,       # PLATFORM_URL, e.g. http://core-app:8080
#            tenant_id=settings.default_tenant_id,
#        )
#        token = await platform.get_oauth_token("google")   # raises if not connected
#        headers = {"Authorization": f"Bearer {token}"}
#
# 2. Persisting nanosecond mtimes or any large integer? Map the column to
#    BigInteger. A nanosecond epoch (~1.8e18) overflows Postgres INTEGER (int32)
#    even though SQLite silently tolerates it — so unit tests pass and prod fails:
#
#        from sqlalchemy import BigInteger
#        from sqlalchemy.orm import Mapped, mapped_column
#
#        mtime_ns: Mapped[int] = mapped_column(BigInteger)   # NOT Integer
