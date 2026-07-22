"""Core-emitted spine events (#665): the file space and suggestion decisions.

This is the **emit** side of the spine inside core-app — the intake/log side lives in
:mod:`epicurus_core_app.event_log`. The core emits over its own bus exactly like a
module would, so its events flow through the same intake → durable log → feed →
automations path as everyone else's, with no private shortcut.

Two families:

* ``files.*`` — the core owns the file space (#434), so file events are core-emitted at
  the file-API seam (:mod:`epicurus_core_app.files_routes`): every mutation door —
  operator upload/delete, module-bridge write/delete/move, object-store fallbacks —
  passes through those handlers. There is deliberately **no** ``file_updated``: an
  overwrite of an existing path emits nothing, so module content that mirrors into the
  file space (notes' ``.md`` mirror, knowledge's vault) does not double-signal every
  ``*.doc_updated`` / ``note_updated`` with a file event. Out-of-band disk changes (the
  #390 watcher's territory) are likewise not emitted — the seam is the API, per #665.
* ``core.suggestion_approved`` / ``core.suggestion_rejected`` — emitted at
  :meth:`~epicurus_core_app.modules.ModuleRegistry.review_action`, the one funnel every
  review surface passes through (module pages over HTTP *and* the in-process core
  pseudo-module, ADR-0093 §2), so a single emission point covers them all.

Every emission is best-effort: a spine hiccup is logged and never fails the mutation or
decision that already landed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from epicurus_core import EntityRef, EventBus, emit_event, get_logger

log = get_logger("epicurus_core_app.core_events")

FILE_ADDED = "files.file_added"
FILE_DELETED = "files.file_deleted"
FILE_MOVED = "files.file_moved"
SUGGESTION_APPROVED = "core.suggestion_approved"
SUGGESTION_REJECTED = "core.suggestion_rejected"


class CoreEventEmitter:
    """Emits the core's own ``files.*`` and ``core.suggestion_*`` spine events.

    ``bus=None`` disables emission entirely (tests / a router built without NATS).
    Methods take the per-request tenant — the file routes are tenant-parameterised, so
    the emitter must not bake one in (constraint #1).
    """

    def __init__(self, bus: EventBus | None) -> None:
        self._bus = bus

    # ── the file space (#434) ────────────────────────────────────────────────

    async def file_added(self, tenant: str, path: str, *, size: int | None = None) -> None:
        """A file came into existence via upload or a module/agent write of a new path."""
        payload: dict[str, Any] = {"path": path}
        if size is not None:
            payload["size"] = size
        await self._emit(
            tenant,
            module="files",
            event_type=FILE_ADDED,
            dedup_key=f"{path}:added:{datetime.now(UTC).isoformat()}",
            payload=payload,
            entity_ref=self._file_ref(path),
        )

    async def file_deleted(self, tenant: str, path: str) -> None:
        """An entry was deleted (one event per API action — a folder takes its subtree)."""
        await self._emit(
            tenant,
            module="files",
            event_type=FILE_DELETED,
            dedup_key=f"{path}:deleted:{datetime.now(UTC).isoformat()}",
            payload={"path": path},
            entity_ref=self._file_ref(path),
        )

    async def file_moved(self, tenant: str, src: str, dst: str) -> None:
        """An entry moved/renamed (file space or the object-store fallback)."""
        await self._emit(
            tenant,
            module="files",
            event_type=FILE_MOVED,
            dedup_key=f"{src}->{dst}:{datetime.now(UTC).isoformat()}",
            payload={"from_path": src, "to_path": dst},
            entity_ref=self._file_ref(dst),
        )

    # ── suggestion decisions (#542 / ADR-0090 / ADR-0093) ────────────────────

    async def suggestion_decided(
        self,
        tenant: str,
        *,
        module: str,
        page_id: str,
        suggestion_id: str,
        result: dict[str, Any],
    ) -> None:
        """One decision event per resolved suggestion, from any review surface.

        *result* is the surface's ``ApplyResult``-shaped response (a plain dict — for an
        external module it crossed HTTP); ``status``/``operation``/``path`` are read
        defensively so a nonconforming module still yields a well-formed event.
        """
        status = str(result.get("status", ""))
        if status not in ("approved", "rejected"):
            log.warning(
                "suggestion decision with unrecognized status; event skipped",
                module=module,
                suggestion_id=suggestion_id,
                status=status,
            )
            return
        event_type = SUGGESTION_APPROVED if status == "approved" else SUGGESTION_REJECTED
        path = str(result.get("path", ""))
        payload: dict[str, Any] = {
            "module": module,
            "page": page_id,  # the suggestion kind: which review queue it came from
            "sid": suggestion_id,
            "operation": str(result.get("operation", "")),
        }
        if path:
            payload["path"] = path
        await self._emit(
            tenant,
            module="core",
            event_type=event_type,
            # A suggestion resolves exactly once (the pending row is dropped, ADR-0033),
            # so the sid+action pair is already deterministic per change.
            dedup_key=f"{module}:{suggestion_id}:{status}",
            payload=payload,
            entity_ref=EntityRef(
                ref_id=suggestion_id,
                module=module,
                kind="suggestion",
                title=path or suggestion_id,
            ),
        )

    # ── internals ────────────────────────────────────────────────────────────

    def _file_ref(self, path: str) -> EntityRef:
        # The Files page has no resolver; the ref still names the entry so a feed row
        # shows a chip, with the hover-card falling back to the ref's own title.
        name = path.rsplit("/", 1)[-1] or path
        return EntityRef(ref_id=path, module="files", kind="file", title=name[:200])

    async def _emit(
        self,
        tenant: str,
        *,
        module: str,
        event_type: str,
        dedup_key: str,
        payload: dict[str, Any],
        entity_ref: EntityRef | None,
    ) -> None:
        """Fire one event, best-effort — a spine hiccup never fails the change it reports."""
        if self._bus is None:
            return
        try:
            await emit_event(
                self._bus,
                tenant_id=tenant,
                module=module,
                event_type=event_type,
                dedup_key=dedup_key,
                payload=payload,
                entity_ref=entity_ref,
            )
        except Exception as exc:
            # `event=` is structlog's reserved key for the message itself — use event_type.
            log.warning("spine emit failed", event_type=event_type, error=str(exc))


__all__ = [
    "FILE_ADDED",
    "FILE_DELETED",
    "FILE_MOVED",
    "SUGGESTION_APPROVED",
    "SUGGESTION_REJECTED",
    "CoreEventEmitter",
]
