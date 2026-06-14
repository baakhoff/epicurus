"""Durable persistence of chat uploads to the storage module — the upload sink (ADR-0025).

The attachment upload route keeps a core-side handle (used to expand a ``file``
attachment into the turn) and, in parallel, pushes the bytes here so they land in the
storage module's object store and become browsable in the Files page. Persistence is
**best-effort**: a sink failure is logged and the upload still succeeds core-side, so a
down or absent storage module never breaks chat.
"""

from __future__ import annotations

import httpx

from epicurus_core import get_logger

log = get_logger("epicurus_core_app.agent.attachment_sink")

_DEFAULT_CONTENT_TYPE = "application/octet-stream"


class AttachmentSink:
    """POSTs uploaded bytes to a storage-like module's ``/ingest`` endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        # Injected by tests (an ASGI transport); a real network transport when None.
        self._transport = transport

    async def persist(
        self,
        *,
        tenant: str,
        att_id: str,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> None:
        """Durably store one uploaded file in the sink module.

        Raises on transport / HTTP error so the caller can log-and-continue; the bytes
        are sent raw with their media type in ``Content-Type`` and the core attachment
        id as the uniqueness token.
        """
        async with httpx.AsyncClient(
            base_url=self._base, timeout=self._timeout, transport=self._transport
        ) as client:
            resp = await client.post(
                "/ingest",
                params={"att_id": att_id, "filename": filename},
                content=data,
                headers={
                    "content-type": content_type or _DEFAULT_CONTENT_TYPE,
                    # Forward the caller's tenant for forward-compatibility; the
                    # single-tenant storage module currently keys uploads off its own
                    # default tenant (threading it end-to-end is a module-wide follow-up).
                    "x-epicurus-tenant": tenant,
                },
            )
            resp.raise_for_status()
        log.info(
            "upload persisted to storage sink", att_id=att_id, filename=filename, tenant=tenant
        )
