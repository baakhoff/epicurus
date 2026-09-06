"""Knowledge module data portability — the module half of the tenant archive (#867, #873).

The knowledge module's only *source-of-truth* tenant data outside the vault itself is the
operator-review queue: pending **suggestions** (ADR-0033, agent-proposed vault changes
awaiting approval) and the resolved-decision **audit trail** (ADR-0090). Both are carried
here, upserted by their own stable id — the ``sid`` each already carries, minted once at
:meth:`~epicurus_knowledge.suggestions.SuggestionStore.add` and never reused, so it is
already exactly the "natural id" ADR-0133 asks for; no derived or surrogate key needed.

Everything else the module owns is **derived** and excluded — see
``docs/services/knowledge.md`` § "Portability" for the full list and why. The vault's own
content travels in the archive's ``files/`` tree (the core's own half of #867), not here;
after import, the core's file rescan and the re-embed fan-out (#332, #848) rebuild the doc
index and the vectors from what landed on disk.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from epicurus_core import ImportReport, PortabilityRecord
from epicurus_knowledge.suggestions import SuggestionAuditStore, SuggestionStore

SCHEMA = "knowledge/1"
"""This module's record schema — bumped only when a record's shape changes incompatibly."""

SUGGESTION_KIND = "suggestion"
DECISION_KIND = "suggestion_decision"


class KnowledgePortability:
    """Implements :class:`epicurus_core.PortabilityStore` for the ``knowledge`` module."""

    schema = SCHEMA

    def __init__(self, suggestions: SuggestionStore, audit: SuggestionAuditStore) -> None:
        self._suggestions = suggestions
        self._audit = audit

    async def export(self, *, tenant_id: str) -> AsyncIterator[PortabilityRecord]:
        """Stream this tenant's pending suggestions, then its resolved-decision history."""
        async for data in self._suggestions.portable_export(tenant=tenant_id):
            yield PortabilityRecord(kind=SUGGESTION_KIND, id=data.pop("sid"), data=data)
        async for data in self._audit.portable_export(tenant=tenant_id):
            yield PortabilityRecord(kind=DECISION_KIND, id=data.pop("sid"), data=data)

    async def import_(
        self,
        *,
        tenant_id: str,
        records: AsyncIterator[PortabilityRecord],
        dry_run: bool,
    ) -> ImportReport:
        """Upsert each record by its ``id`` (the stable ``sid``); unknown kinds are skipped."""
        report = ImportReport(schema_name=self.schema)
        async for record in records:
            if record.kind == SUGGESTION_KIND:
                outcome = await self._suggestions.portable_upsert(
                    tenant=tenant_id, sid=record.id, data=record.data, dry_run=dry_run
                )
            elif record.kind == DECISION_KIND:
                outcome = await self._audit.portable_upsert(
                    tenant=tenant_id, sid=record.id, data=record.data, dry_run=dry_run
                )
            else:
                report.warn(f"unknown record kind {record.kind!r}; skipped")
                outcome = "skipped"
            report.record(record.kind, outcome)
        return report


__all__ = ["DECISION_KIND", "SCHEMA", "SUGGESTION_KIND", "KnowledgePortability"]
