"""Mail module data portability — the module half of the tenant export/import contract (#867).

Verified against the actual mail schema (:mod:`epicurus_mail.db`) before writing a line of
this file: every table the module persists — ``mail_thread``, ``mail_label``, ``mail_sync``,
``mail_landing``, ``mail_category`` — is a Gmail-derived cache (ADR-0096, #623/#765), never
source-of-truth data. Mail is the one module with no local provider (ADR-0032): Gmail *is*
the mailbox, and every one of those tables is wholesale rebuilt by the next full sync or
reconcile tick if it were dropped entirely — ``mail_landing``'s "next cursor" and
``mail_category``'s TTL'd tab cache included. ADR-0133 excludes a derived index or provider
cache/mirror from export, which leaves nothing in this module worth carrying between
installations: :class:`MailPortability` reports the schema and a live route pair, but its
export stream is a header with zero records, and its import treats any record it is handed
as a kind it has never heard of.

The store still implements the full :class:`~epicurus_core.PortabilityStore` protocol
(rather than the module leaving ``portable=False``, which the contract also supports for a
module with nothing to carry) so mail behaves identically to every other module in an
export/import run: an operator sees it listed with an honest zero count instead of silently
missing, and the plumbing is ready the day mail gains a genuine per-tenant setting (a
signature, a muted-sender list, a per-label triage rule — none of which exist today) worth
carrying.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from epicurus_core import ImportReport, PortabilityRecord

SCHEMA = "mail/1"


class MailPortability:
    """``PortabilityStore`` for the mail module.

    Every persisted mail table is a Gmail-derived cache (see the module docstring), so there
    is no source-of-truth record kind to export or import today.
    """

    schema = SCHEMA

    async def export(self, *, tenant_id: str) -> AsyncIterator[PortabilityRecord]:
        """Yield nothing — mail holds no source-of-truth tenant data (see module docstring).

        ``tenant_id`` is accepted to satisfy the ``PortabilityStore`` protocol and because a
        future record kind will need it; the empty stream is the same for every tenant today.
        """
        return
        yield  # pragma: no cover - makes this an async generator; the return above always fires

    async def import_(
        self,
        *,
        tenant_id: str,
        records: AsyncIterator[PortabilityRecord],
        dry_run: bool,
    ) -> ImportReport:
        """Count every incoming record as an unrecognized kind — mail declares none.

        ``tenant_id`` and ``dry_run`` are accepted for protocol conformance; nothing is ever
        written either way, since this store recognizes no ``kind`` to write.
        """
        report = ImportReport(schema_name=self.schema)
        async for record in records:
            report.warn(f"unknown record kind {record.kind!r}: mail exports no record kinds")
            report.record(record.kind, "skipped")
        return report
