"""MCP module base — the building block for a sidecar module's tool contract.

Wraps the MCP SDK's ``FastMCP`` with epicurus conventions: register tools, declare
the events the module emits/consumes, generate the module manifest, and expose the
HTTP (streamable-http) app to serve over the internal Docker network. The contract
is local-only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from epicurus_core.manifest import (
    CONTRACT_VERSION,
    AutomationTemplate,
    CollectionsSpec,
    EventSpec,
    ModelSlot,
    ModuleManifest,
    PageSpec,
    SideEffect,
    ToolSpec,
    UiSection,
    WritesDocument,
)

__all__ = ["EpicurusModule", "add_manifest_route"]

Decorator = Callable[[Callable[..., Any]], Callable[..., Any]]


class EpicurusModule:
    """A sidecar module: MCP tools exposed to the agent, plus declared events.

    >>> module = EpicurusModule("greeter", version="1.0.0")
    >>> @module.tool()
    ... def greet(name: str) -> str:
    ...     return f"hello {name}"
    """

    def __init__(
        self,
        name: str,
        *,
        version: str = "0.1.0",
        description: str = "",
        instructions: str | None = None,
        image: str | None = None,
        config: list[str] | None = None,
        secrets: list[str] | None = None,
        ui: UiSection | None = None,
        pages: list[PageSpec] | None = None,
        resolver: bool = False,
        attachable: bool = False,
        required_models: list[ModelSlot] | None = None,
        collections: CollectionsSpec | None = None,
        oauth_scopes: dict[str, list[str]] | None = None,
        docs_url: str | None = None,
        reindexable: bool = False,
        automation_templates: list[AutomationTemplate] | None = None,
    ) -> None:
        self._name = name
        self._version = version
        self._description = description
        self._image = image
        self._config = list(config or [])
        self._secrets = list(secrets or [])
        self._ui = ui
        self._pages = list(pages or [])
        self._resolver = resolver
        self._attachable = attachable
        self._required_models = list(required_models or [])
        self._collections = collections
        self._oauth_scopes = dict(oauth_scopes or {})
        self._docs_url = docs_url
        self._reindexable = reindexable
        # Preset automations offered on the shell's Templates tab (ADR-0105). Declaring one
        # never creates anything: the operator instantiates it, so installing a module can
        # never make the assistant start acting on its own.
        self._automation_templates = list(automation_templates or [])
        self._mcp = FastMCP(
            name,
            instructions=instructions,
            # Serve MCP at the app root so mounting at "/mcp" yields a clean endpoint
            # (the default "/mcp" path would become "/mcp/mcp" once mounted).
            streamable_http_path="/",
            # The module<->agent contract is local-only on the internal Docker network
            # (ADR-0004); DNS-rebinding protection would reject service hostnames like
            # "echo:8080" with HTTP 421 and block agent-to-module calls, so disable it.
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        self._events_emitted: list[EventSpec] = []
        self._events_consumed: list[EventSpec] = []
        # Document-pane annotations by tool name (#541, ADR-0100) — folded into the ToolSpecs
        # in ``manifest()``, since FastMCP owns the tool registry and knows nothing of them.
        self._writes_documents: dict[str, WritesDocument] = {}
        self._side_effects: dict[str, SideEffect] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def mcp(self) -> FastMCP:
        """The underlying FastMCP server (for advanced use / testing)."""
        return self._mcp

    def tool(
        self,
        name: str | None = None,
        description: str | None = None,
        *,
        writes_document: WritesDocument | None = None,
        side_effect: SideEffect | None = None,
    ) -> Decorator:
        """Register a tool (decorator). Delegates to FastMCP.

        ``writes_document`` opts the tool into the shell's live document pane by naming the
        arguments the document travels in (#541, ADR-0100) — declared here, beside the tool it
        describes, and attached to its :class:`ToolSpec` when the manifest is built. The names
        are checked against the tool's generated input schema then, so a typo fails at
        manifest-build time rather than surfacing as a pane that never fills.

        ``side_effect`` declares what the tool does to the world — ``read`` / ``propose`` /
        ``write`` (ADR-0105). An automation's autonomy level derives its tool allowance from
        it, enforced at the turn's tool surface. Omitting it means ``write``: the most
        restrictive reading, so an unannotated tool is withheld from a read-only automation
        rather than trusted by one. **Annotate your read tools** — that is what makes them
        usable by a Notify automation.
        """
        registered = self._mcp.tool(name=name, description=description)
        if writes_document is None and side_effect is None:
            return registered

        def annotate(fn: Callable[..., Any]) -> Callable[..., Any]:
            # Key by the name FastMCP will publish: the explicit one, else the function's.
            key = name or fn.__name__
            if writes_document is not None:
                self._writes_documents[key] = writes_document
            if side_effect is not None:
                self._side_effects[key] = side_effect
            return registered(fn)

        return annotate

    def emits(self, subject: str, description: str = "") -> None:
        """Declare a base event subject this module publishes."""
        self._events_emitted.append(EventSpec(subject=subject, description=description))

    def consumes(self, subject: str, description: str = "") -> None:
        """Declare a base event subject this module subscribes to."""
        self._events_consumed.append(EventSpec(subject=subject, description=description))

    async def manifest(
        self,
        *,
        config: list[str] | None = None,
        secrets: list[str] | None = None,
    ) -> ModuleManifest:
        """Build the manifest from the registered tools and declared events.

        ``config``/``secrets`` override what was declared at construction.
        """
        tools = [
            ToolSpec(
                name=t.name,
                description=t.description or "",
                input_schema=t.inputSchema,
                writes_document=self._writes_documents.get(t.name),
                # Absent → "write", the ToolSpec default: the most restrictive reading.
                **(
                    {"side_effect": self._side_effects[t.name]}
                    if t.name in self._side_effects
                    else {}
                ),
            )
            for t in await self._mcp.list_tools()
        ]
        # An annotation whose tool never registered would be silently dropped here, leaving the
        # pane mysteriously dead; say so instead. Catches a renamed tool that outran its
        # annotation, or a name that didn't survive FastMCP's registration.
        registered_names = {t.name for t in tools}
        unregistered = sorted(self._writes_documents.keys() - registered_names)
        if unregistered:
            raise ValueError(
                f"module {self._name!r}: writes_document declared for unregistered tool(s) "
                f"{unregistered}"
            )
        # Same reasoning for side_effect, and it matters more: a read annotation that silently
        # missed its tool would quietly demote that tool to "write" and drop it out of every
        # Notify automation's reach — a feature failing shut, invisibly.
        unclassified = sorted(self._side_effects.keys() - registered_names)
        if unclassified:
            raise ValueError(
                f"module {self._name!r}: side_effect declared for unregistered tool(s) "
                f"{unclassified}"
            )
        return ModuleManifest(
            name=self._name,
            version=self._version,
            description=self._description,
            contract_version=CONTRACT_VERSION,
            image=self._image,
            tools=tools,
            events_emitted=list(self._events_emitted),
            events_consumed=list(self._events_consumed),
            config=config if config is not None else self._config,
            secrets=secrets if secrets is not None else self._secrets,
            ui=self._ui,
            pages=list(self._pages),
            resolver=self._resolver,
            attachable=self._attachable,
            required_models=list(self._required_models),
            collections=self._collections,
            oauth_scopes=dict(self._oauth_scopes),
            docs_url=self._docs_url,
            reindexable=self._reindexable,
            automation_templates=list(self._automation_templates),
        )

    def http_app(self) -> Starlette:
        """ASGI app serving MCP over streamable HTTP (internal Docker network only)."""
        return self._mcp.streamable_http_app()


def add_manifest_route(app: FastAPI, module: EpicurusModule) -> None:
    """Serve the module's manifest at ``GET /manifest``.

    The core's module registry reads this to surface the module — tools, events,
    and its declarative UI — to the agent and the web shell (ADR-0004 / ADR-0007).
    """

    @app.get("/manifest", response_model=ModuleManifest)
    async def manifest() -> ModuleManifest:
        return await module.manifest()
