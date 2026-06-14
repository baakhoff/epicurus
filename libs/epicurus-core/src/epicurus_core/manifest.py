"""Module manifest — the descriptor every epicurus module ships (ADR-0004).

It declares the module's identity, the tools it serves to the agent (MCP), the
events it emits and consumes (NATS), and the config/secrets it needs. The service
template generates it; the future one-click installer (Phase 7) reads it to add a
module by URL.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

CONTRACT_VERSION = "0.1"
"""Version of the module<->core contract this manifest targets."""

__all__ = [
    "CONTRACT_VERSION",
    "EventSpec",
    "ModuleManifest",
    "PageArchetype",
    "PageSpec",
    "ToolSpec",
    "UiAction",
    "UiSection",
]

PageArchetype = Literal["browser", "calendar", "editor", "board"]
"""The bounded set of left-nav view shapes the shell can render (ADR-0018).

Core-owned and core-rendered: ``browser`` (tree/list + detail), ``calendar``,
``editor`` (Obsidian-like doc), ``board`` (lists/cards). A module names one of these
and supplies data; it never ships markup, and it cannot invent a new shape — the
vocabulary extends only in core.
"""


class ToolSpec(BaseModel):
    """A tool the module exposes to the agent over MCP."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class EventSpec(BaseModel):
    """A NATS event the module emits or consumes.

    ``subject`` is the *base* subject; it is tenant-scoped at runtime via
    ``scope_subject`` (``<tenant>.<subject>``).
    """

    subject: str
    description: str = ""


class UiAction(BaseModel):
    """A button the web shell renders for a module; pressing it invokes an MCP tool.

    The shell builds the input form from the tool's own ``input_schema`` — the same
    JSON-Schema vocabulary as tool calls, so an action needs no extra schema here.
    ``intent`` styles the button; a ``danger`` action must set ``confirm`` (the
    confirmation prompt shown before it runs).
    """

    tool: str
    label: str
    description: str = ""
    intent: Literal["default", "primary", "danger"] = "default"
    confirm: str | None = None

    @model_validator(mode="after")
    def _danger_requires_confirm(self) -> UiAction:
        # The contract: a destructive action must carry a confirmation prompt, so the
        # shell never renders a one-tap "danger" button. Enforced, not just documented.
        if self.intent == "danger" and not self.confirm:
            raise ValueError("a danger action must set a confirm prompt")
        return self


class UiSection(BaseModel):
    """The module's declarative UI (ADR-0007 Tier 1).

    The web shell auto-renders this — installing a module surfaces its
    config/status/actions with **no core-UI rebuild** and **no module JS** in the
    shell. ``ui_version`` versions this vocabulary independently of the wire
    contract (additive fields don't bump it; a shell that sees an unknown version
    falls back to a plain card). ``icon`` names a glyph from the shell's vendored
    icon set — never an image URL or script. ``config_schema`` is a JSON Schema
    (object) the shell renders as the module's settings form; values round-trip
    through the core. ``status_url`` is a relative path on the module (e.g.
    ``/status``) that returns live status data as a flat JSON object; the core
    proxies it at ``GET /platform/v1/modules/{name}/status`` so the shell never
    calls a module directly. ``ui_url`` opts into Tier 2 (a module-served page in a
    sandboxed iframe) — reserved, not yet rendered by the shell.
    """

    ui_version: str = "1"
    icon: str = "puzzle"
    summary: str = ""
    config_schema: dict[str, Any] | None = None
    actions: list[UiAction] = Field(default_factory=list)
    status_url: str | None = None
    ui_url: str | None = None


class PageSpec(BaseModel):
    """A left-nav page a module contributes — core-rendered from a bounded vocabulary (ADR-0018).

    The module supplies **data only** and names which core archetype presents it;
    the shell owns all chrome and styling. The shell fetches the page's data from
    the module through the core proxy at ``GET /platform/v1/modules/{module}/pages/{id}``
    (the module serves it at ``GET /pages/{id}`` in the archetype's data shape) — the
    shell never calls a module directly.

    ``id`` is unique within the module and forms the page's data path and nav route;
    ``icon`` names a glyph from the shell's vendored set (never an image URL or
    script); ``nav_order`` sorts the entry in the left nav (lower is higher);
    ``capability`` is an optional gate the shell may check before showing the page
    (reserved — e.g. a connected account — not yet enforced).
    """

    id: str
    title: str
    archetype: PageArchetype
    icon: str = "puzzle"
    nav_order: int = 100
    capability: str | None = None


class ModuleManifest(BaseModel):
    """The full descriptor a module publishes about itself."""

    name: str
    version: str
    description: str = ""
    contract_version: str = CONTRACT_VERSION
    # Container image — populated for distribution / the installer.
    image: str | None = None
    tools: list[ToolSpec] = Field(default_factory=list)
    events_emitted: list[EventSpec] = Field(default_factory=list)
    events_consumed: list[EventSpec] = Field(default_factory=list)
    # Names of non-secret config keys and OpenBao secrets the module requires.
    config: list[str] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    # Declarative UI the web shell renders for this module (ADR-0007 Tier 1).
    ui: UiSection | None = None
    # Left-nav pages, core-rendered from the bounded archetype vocabulary (ADR-0018).
    pages: list[PageSpec] = Field(default_factory=list)
    # The module serves a hover-card resolver at ``GET /resolve/{kind}/{ref_id}`` for the
    # entities it references in chat (ADR-0019); the core proxies it.
    resolver: bool = False
