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
    "AutomationTemplate",
    "CollectionsSpec",
    "EventSpec",
    "ModelRole",
    "ModelSlot",
    "ModuleManifest",
    "PageArchetype",
    "PageSpec",
    "SideEffect",
    "ToolSpec",
    "UiAction",
    "UiSection",
    "WritesDocument",
]

PageArchetype = Literal["browser", "calendar", "editor", "board", "review", "mailbox"]
"""The bounded set of left-nav view shapes the shell can render (ADR-0018).

Core-owned and core-rendered: ``browser`` (tree/list + detail), ``calendar``,
``editor`` (Obsidian-like doc), ``board`` (lists/cards), ``review`` (suggestion queue),
and ``mailbox`` (labels rail -> paginated thread list -> conversation + compose/reply,
ADR-0087). A module names one of these and supplies data; it never ships markup, and it
cannot invent a new shape — the vocabulary extends only in core.
"""


class WritesDocument(BaseModel):
    """Marks a tool as *writing a document*, and says which arguments carry it (ADR-0100).

    The seam behind the shell's live document pane (#541): a module declares that a tool
    produces a document and names the arguments it travels in, and the shell opens the pane
    beside chat when the agent calls it — for **any** module, with no per-module code in the
    web (ADR-0018/0019: the module supplies data, the shell renders).

    It is an annotation, not a capability: the tool keeps its own name, schema, and behavior,
    and a core that doesn't understand this field simply ignores it. Only ``content_arg`` is
    required — it names the argument holding the document body. ``title_arg`` names a human
    title for the pane header, ``target_arg`` the document the write lands in (a path or id),
    so the pane can show what is being written before the tool returns; either may be omitted
    when the tool has no such argument, and the shell falls back to what the result carries.
    """

    content_arg: str = Field(min_length=1)
    title_arg: str | None = None
    target_arg: str | None = None

    def named_args(self) -> list[str]:
        """The tool arguments this annotation points at (``content_arg`` first)."""
        return [a for a in (self.content_arg, self.title_arg, self.target_arg) if a is not None]


SideEffect = Literal["read", "propose", "write"]
"""What a tool *does to the world* — the vocabulary the automations autonomy dial gates on.

Three classes, because two cannot express the dial (ADR-0105):

* ``read`` — observes and changes nothing (``mail_search``, ``calendar_list``, ``now``).
* ``propose`` — **stages for human approval by construction**, never applies on its own:
  ``mail_send`` composes a :class:`~epicurus_core.contracts.DraftReview` (the draft-first
  guarantee, ADR-0085), ``knowledge_propose_*`` stages a suggestion (#305). The tool
  cannot transmit or commit even if the model wants it to.
* ``write`` — applies directly (``calendar_create_event``, ``mail_mark_read``).

The default is ``write``, and that direction is deliberate: an unannotated tool is
invisible to a read-only automation rather than silently trusted by one. A missing
annotation costs availability, never containment.

This is *not* a naming heuristic — those break immediately (``mail_mark_read`` reads
nothing; it mutates). It is declared beside the tool, by the author who knows.
"""


class ToolSpec(BaseModel):
    """A tool the module exposes to the agent over MCP."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    # Opt in to the shell's live document pane by naming the args the document travels in
    # (#541, ADR-0100). Absent on the vast majority of tools — a tool that writes no document
    # simply omits it, and its calls render as they always have.
    writes_document: WritesDocument | None = None
    # What this tool does to the world (ADR-0105). An automation's autonomy level derives its
    # tool allowance from this, enforced at the turn's tool surface — so a Notify automation is
    # not *asked* to avoid writing, it is handed no tool that can. Defaults to the most
    # restrictive reading (``write``), so forgetting to annotate a read tool costs its
    # availability rather than the guarantee.
    side_effect: SideEffect = "write"

    @model_validator(mode="after")
    def _writes_document_names_real_args(self) -> ToolSpec:
        # The annotation is only useful if it points at arguments the tool actually takes; a
        # typo would otherwise surface as a pane that silently never fills. Checked here so a
        # module fails at manifest-build time — the same "enforced, not just documented"
        # posture as UiAction's danger/confirm rule. Skipped when the tool declares no
        # properties to check against (input_schema is optional).
        if self.writes_document is None:
            return self
        properties = self.input_schema.get("properties")
        if not isinstance(properties, dict):
            return self
        unknown = [arg for arg in self.writes_document.named_args() if arg not in properties]
        if unknown:
            raise ValueError(
                f"tool {self.name!r}: writes_document names {unknown}, "
                f"which are not arguments of its input_schema"
            )
        return self


class EventSpec(BaseModel):
    """A NATS event the module emits or consumes.

    ``subject`` is the *base* subject; it is tenant-scoped at runtime via
    ``scope_subject`` (``<tenant>.<subject>``).
    """

    subject: str
    description: str = ""


ModelRole = Literal["embedding", "chat"]
"""The kind of model a slot needs — an embedding model or a chat/LLM (#128)."""


class ModelSlot(BaseModel):
    """A model the module needs the operator to choose (#128, ADR-0029).

    The module declares slots; the user picks which model fills each in the shell; the
    core stores the choice and the module fetches it via ``PlatformClient.get_module_model``
    and passes it to ``embed`` / ``chat``. An unset slot falls back to the core default.
    """

    key: str
    role: ModelRole
    label: str
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


class AutomationTemplate(BaseModel):
    """A preset automation a module suggests, for the shell's Templates tab (ADR-0105).

    A module knows what is worth automating about itself — "tell me when mail arrives from
    someone I've replied to before", "summarize tomorrow's calendar each evening" — far
    better than the core does. Declaring it here surfaces it as a **starting point the
    operator instantiates**, never a live automation: a module cannot create one, and
    installing a module must never make the assistant start doing things unasked. That is
    a product decision (owner-decided, Templates tab, never auto-instantiated), and the
    contract enforces it by carrying no "enabled" field to set.

    An instantiated template becomes an ordinary automation row with
    ``source="template:<module>"``, and the operator then owns it — later edits to the
    module's template do not reach back into it.

    ``trigger`` and ``sinks`` are deliberately loose (``dict``/``list[str]``): the core's
    automations model owns that vocabulary and validates the shape when a template is
    instantiated, so a module pinned to an older library cannot break the core's parse by
    declaring a field it has since renamed. A template that fails validation is skipped
    with a warning, not fatal.
    """

    key: str = Field(min_length=1)  # unique within the module; identifies the template
    name: str = Field(min_length=1)  # the automation's name when instantiated
    description: str = ""  # what it does, shown on the template card
    trigger: dict[str, Any] = Field(default_factory=dict)  # the core's trigger vocabulary
    prompt: str = ""  # the agent step's instructions
    autonomy: str = "notify"  # the level it is *suggested* at; the operator may change it
    sinks: list[str] = Field(default_factory=list)  # e.g. ["chat"], ["push", "notes"]


class CollectionsSpec(BaseModel):
    """A module's account/collection capability (ADR-0030).

    Declaring this opts the module into the account/collection model: it serves
    ``GET /accounts`` (its connected accounts and their collections) and reads the
    operator's selection via ``PlatformClient.get_collections``. The shell renders a
    connected-accounts section — per-collection on/off toggles, an active switcher, and a
    Connect affordance for each provider in ``providers``. ``noun`` labels a collection in
    the UI (``"calendar"`` → "Calendars", ``"list"`` → "Lists"); ``multi`` is True when
    reads overlay every enabled collection (calendar) and False when only the active one
    is shown (tasks). ``local`` is always the silent fallback and is never listed here.
    """

    noun: str
    multi: bool = False
    providers: list[str] = Field(default_factory=list)


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
    # Free-text tags for browsing/filtering modules in the shell (by name, description,
    # or tag — #126). Purely descriptive; the core never routes on them.
    tags: list[str] = Field(default_factory=list)
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
    # The module is a chat-attachment source: it serves a picker (``GET /attachments``) and
    # a resolve (``GET /attachments/{ref_id}``) so its entities can be attached (ADR-0019).
    attachable: bool = False
    # Model "slots" the operator fills via the shell (#128): the module fetches its chosen
    # model with ``PlatformClient.get_module_model`` and passes it to embed/chat; an unset
    # slot falls back to the core default.
    required_models: list[ModelSlot] = Field(default_factory=list)
    # The module backs itself with a silent ``local`` default plus 0+ connected external
    # accounts whose collections the operator toggles/switches (ADR-0030). When set, the
    # module serves ``GET /accounts`` and reads its selection via
    # ``PlatformClient.get_collections``; the shell renders the connected-accounts section.
    collections: CollectionsSpec | None = None
    # OAuth API scopes the module needs, per provider (#241): ``{provider: [scope, …]}`` —
    # e.g. ``{"google": ["https://www.googleapis.com/auth/calendar"]}``. The shell unions
    # these across modules and requests them at connect (``?scope=``) so connecting an
    # account grants the API access its modules require; the core always adds the default
    # identity scopes and accumulates grants (``include_granted_scopes``). Empty = the module
    # needs only the default identity scopes.
    oauth_scopes: dict[str, list[str]] = Field(default_factory=dict)
    # A relative path on the module (e.g. ``/module-docs``) that returns documentation pages for
    # the knowledge module to auto-index (#215). Response shape:
    # ``{"documents": [{"path": "usage.md", "content": "..."}]}``. The core proxies this at
    # ``GET /platform/v1/modules/{name}/docs``. Do NOT use ``/docs`` — it is FastAPI's built-in
    # Swagger UI and would shadow the route. Omit when the module has no docs to contribute.
    docs_url: str | None = None
    # The module re-embeds its corpus on demand: it serves ``POST /reindex`` (drop + rebuild its
    # Qdrant collection with the current embedding model), and the core's re-embed fan-out
    # (#332) calls it when the operator changes the embedding model. Modules that hold no
    # embeddings leave this ``False``.
    reindexable: bool = False
    # Preset automations this module suggests (ADR-0105). Offered on the shell's Templates tab
    # as starting points the operator instantiates — **never auto-instantiated**, so installing
    # a module cannot make the assistant start acting on its own.
    automation_templates: list[AutomationTemplate] = Field(default_factory=list)
