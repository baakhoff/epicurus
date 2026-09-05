"""Tenant data portability — export one epicurus, import it into another (#866 / #867).

A **logical, tenant-scoped** archive: the operator's own data, in a format they can open,
moved between installations through the UI. Distinct from ``infra/backups``, which images
the volumes of *one* deployment for disaster recovery — that stays the restore path when a
disk dies; this is the path when a person moves house.

* :mod:`.models` — the archive manifest and the two job views (one vocabulary, three uses).
* :mod:`.core_data` — which of the core's own tables travel, and which are derived or
  operational and deliberately do not.
* :mod:`.archive` — the ``.tar.gz`` layout, written and read off the event loop.
* :mod:`.jobs` — the durable, tenant-scoped job rows a page reload can find again.
* :mod:`.service` — the orchestrator: fan out, assemble, preview, apply, rebuild.
* :mod:`.routes` — ``/platform/v1/portability``.
"""

from __future__ import annotations

from epicurus_core_app.portability.jobs import PortabilityJob, PortabilityJobStore
from epicurus_core_app.portability.models import (
    PORTABILITY_FORMAT_VERSION,
    ArchiveManifest,
    ComponentEntry,
    ImportPreview,
    ImportReportView,
    SecretsInventory,
)
from epicurus_core_app.portability.routes import create_portability_router
from epicurus_core_app.portability.service import PortabilityService

__all__ = [
    "PORTABILITY_FORMAT_VERSION",
    "ArchiveManifest",
    "ComponentEntry",
    "ImportPreview",
    "ImportReportView",
    "PortabilityJob",
    "PortabilityJobStore",
    "PortabilityService",
    "SecretsInventory",
    "create_portability_router",
]
