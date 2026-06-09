"""Module manifest — the descriptor every epicurus module ships (ADR-0004).

It declares the module's identity, the tools it serves to the agent (MCP), the
events it emits and consumes (NATS), and the config/secrets it needs. The service
template generates it; the future one-click installer (Phase 7) reads it to add a
module by URL.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

CONTRACT_VERSION = "0.1"
"""Version of the module<->core contract this manifest targets."""

__all__ = ["CONTRACT_VERSION", "EventSpec", "ModuleManifest", "ToolSpec"]


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
