"""The reserved ``core`` pseudo-module, serving several in-process review pages (ADR-0107).

ADR-0093 §2 gave the core one in-process ``review`` page — the agent's playbooks — answered by the
:class:`~epicurus_core_app.modules.ModuleRegistry` directly instead of over HTTP. #667 adds a
second: the automations the agent drafts by conversation. The registry seam already fans out over
``manifest().pages`` and dispatches ``get_page``/``review_action``/``review_audit`` by ``page_id``,
so serving two pages needs no registry change — only a single object under the reserved name that
declares both and routes to the right one.

:class:`CorePages` is that object. It owns the aggregate manifest (name, version, ui) and delegates
each call to the page that owns the ``page_id``. Each page stays a focused, single-responsibility
handler (:class:`~epicurus_core_app.agent.playbook_review.CoreReviewPage`,
:class:`~epicurus_core_app.automations.review.CoreAutomationReviewPage`), and the shell cannot tell
this composite apart from a real module serving two review pages.

This is emphatically **not** a second review *surface* (#667): both pages render through the one
unmodified ``ReviewView``/``SuggestionReviewModal``, exactly as knowledge's and notes' queues do,
and both fold into the single Suggestions inbox. It is one more page in that inbox, not a rival.
"""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import HTTPException

from epicurus_core.manifest import ModuleManifest, PageSpec, UiSection


class CoreSubPage(Protocol):
    """One in-process ``review`` page :class:`CorePages` can host.

    The same four-method surface a real module serves over HTTP, minus the manifest — the composite
    owns identity (the reserved name and version) and asks each page only for its :meth:`page_spec`.
    """

    def page_spec(self) -> PageSpec: ...

    async def get_page(self, page_id: str) -> dict[str, Any]: ...

    async def review_action(
        self, page_id: str, suggestion_id: str, action: str, content: str | None = None
    ) -> dict[str, Any]: ...

    async def review_audit(self, page_id: str, *, limit: int = 50) -> dict[str, Any]: ...


class CorePages:
    """The reserved ``core`` pseudo-module: several review pages under one manifest (ADR-0107).

    Implements the ``CorePseudoModule`` protocol the registry consumes (``manifest`` + the three
    page-scoped dispatchers), routing each call to the page that declares the ``page_id``. Page ids
    must be unique across the hosted pages — the constructor is the one place that could collide, so
    it rejects a duplicate loudly rather than letting one page silently shadow another.
    """

    def __init__(
        self,
        *,
        name: str,
        version: str,
        description: str,
        ui: UiSection,
        pages: list[CoreSubPage],
    ) -> None:
        self._name = name
        self._version = version
        self._description = description
        self._ui = ui
        self._pages: dict[str, CoreSubPage] = {}
        for page in pages:
            page_id = page.page_spec().id
            if page_id in self._pages:
                raise ValueError(f"duplicate core page id: {page_id!r}")
            self._pages[page_id] = page

    def manifest(self) -> ModuleManifest:
        """The aggregate manifest — the reserved name, every hosted page, no tools/MCP/config.

        Like the single-page precedent it declares no ``tools``/``events``/``config``/``secrets``:
        the core is not a module and contributes nothing to the agent's tool surface. The ``ui``
        section only gives the Suggestions inbox an icon for the ``core`` group heading.
        """
        return ModuleManifest(
            name=self._name,
            version=self._version,
            description=self._description,
            pages=[page.page_spec() for page in self._pages.values()],
            ui=self._ui,
        )

    async def get_page(self, page_id: str) -> dict[str, Any]:
        return await self._page(page_id).get_page(page_id)

    async def review_action(
        self, page_id: str, suggestion_id: str, action: str, content: str | None = None
    ) -> dict[str, Any]:
        return await self._page(page_id).review_action(page_id, suggestion_id, action, content)

    async def review_audit(self, page_id: str, *, limit: int = 50) -> dict[str, Any]:
        return await self._page(page_id).review_audit(page_id, limit=limit)

    def _page(self, page_id: str) -> CoreSubPage:
        """The page that owns *page_id*, or a 404 — the shape a module's missing page returns."""
        page = self._pages.get(page_id)
        if page is None:
            raise HTTPException(status_code=404, detail=f"core has no page {page_id!r}")
        return page


__all__ = ["CorePages", "CoreSubPage"]
