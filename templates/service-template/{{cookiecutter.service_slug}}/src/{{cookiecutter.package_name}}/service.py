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
#
# 3. Does this module hold data the operator would expect to take with them when
#    they move to another epicurus (#867)? Then it is *portable*: declare the flag
#    and serve the two routes with the shared helper — never hand-roll an export
#    format, and never put a secret in a record (a module holds none; the core
#    reports what to re-enter). The store upserts by a **stable** id, so applying
#    the same archive twice is a no-op:
#
#        from epicurus_core import (
#            ImportReport, PortabilityRecord, add_portability_routes,
#        )
#
#        module = EpicurusModule(MODULE_NAME, version="0.1.0", portable=True)
#
#        class Portability:
#            schema = f"{MODULE_NAME}/1"     # bump when a record's SHAPE changes
#
#            async def export(self, *, tenant_id: str):
#                for row in await store.all(tenant_id):
#                    yield PortabilityRecord(kind="thing", id=row.id, data=row.as_dict())
#
#            async def import_(self, *, tenant_id, records, dry_run) -> ImportReport:
#                report = ImportReport(schema_name=self.schema)
#                async for record in records:
#                    ...                      # upsert by record.id; never delete
#                    report.record(record.kind, "created")
#                return report
#
#        add_portability_routes(app, module, Portability())
