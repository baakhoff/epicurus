"""Tests for the module manifest models."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, Field, ValidationError

from epicurus_core.manifest import (
    CONTRACT_VERSION,
    CollectionsSpec,
    EventSpec,
    ModelSlot,
    ModuleManifest,
    PageSpec,
    ToolSpec,
    UiAction,
    UiSection,
    WritesDocument,
)

_DOC_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "title": {"type": "string"},
        "content": {"type": "string"},
    },
}


def test_defaults() -> None:
    m = ModuleManifest(name="greeter", version="1.0.0")
    assert m.contract_version == CONTRACT_VERSION
    assert m.image is None
    assert m.tools == []
    assert m.events_emitted == []
    assert m.secrets == []
    assert m.required_models == []


def test_model_slots_declared_on_manifest() -> None:
    m = ModuleManifest(
        name="knowledge",
        version="0.6.0",
        required_models=[ModelSlot(key="embedding", role="embedding", label="Embedding model")],
    )
    assert m.required_models[0].key == "embedding"
    assert m.required_models[0].role == "embedding"


def test_model_slot_rejects_unknown_role() -> None:
    with pytest.raises(ValidationError):
        ModelSlot(key="x", role="vision", label="X")  # type: ignore[arg-type]


def test_roundtrip() -> None:
    m = ModuleManifest(
        name="greeter",
        version="1.0.0",
        description="says hi",
        image="ghcr.io/x/greeter:1",
        tools=[ToolSpec(name="greet", description="greet", input_schema={"type": "object"})],
        events_emitted=[EventSpec(subject="greeting.sent", description="after greeting")],
        events_consumed=[EventSpec(subject="inbox.message")],
        config=["GREETING"],
        secrets=["API_KEY"],
    )
    restored = ModuleManifest.model_validate(m.model_dump())
    assert restored == m
    assert restored.tools[0].name == "greet"
    assert restored.events_emitted[0].subject == "greeting.sent"
    assert restored.secrets == ["API_KEY"]


def test_ui_section_defaults() -> None:
    ui = UiSection()
    assert ui.ui_version == "1"
    assert ui.icon == "puzzle"
    assert ui.config_schema is None
    assert ui.actions == []
    assert ui.ui_url is None


def test_danger_action_requires_a_confirm_prompt() -> None:
    with pytest.raises(ValidationError):
        UiAction(tool="purge", label="Purge", intent="danger")
    # with a confirm prompt it validates
    action = UiAction(tool="purge", label="Purge", intent="danger", confirm="Erase everything?")
    assert action.confirm == "Erase everything?"


def test_non_danger_actions_need_no_confirm() -> None:
    assert UiAction(tool="echo", label="Send", intent="primary").confirm is None
    assert UiAction(tool="echo", label="Send").intent == "default"


def test_manifest_with_ui_roundtrips() -> None:
    m = ModuleManifest(
        name="greeter",
        version="1.0.0",
        ui=UiSection(
            summary="says hi",
            config_schema={"type": "object", "properties": {"greeting": {"type": "string"}}},
            actions=[UiAction(tool="greet", label="Greet")],
        ),
    )
    restored = ModuleManifest.model_validate(m.model_dump())
    assert restored == m
    assert restored.ui is not None
    assert restored.ui.actions[0].tool == "greet"
    assert restored.ui.config_schema is not None


def test_page_spec_defaults() -> None:
    page = PageSpec(id="files", title="Files", archetype="browser")
    assert page.icon == "puzzle"
    assert page.nav_order == 100
    assert page.capability is None


def test_page_spec_rejects_unknown_archetype() -> None:
    with pytest.raises(ValidationError):
        PageSpec(id="x", title="X", archetype="kanban")  # type: ignore[arg-type]


def test_manifest_defaults_to_no_pages() -> None:
    assert ModuleManifest(name="m", version="1.0").pages == []


def test_manifest_resolver_and_attachable_default_false() -> None:
    m = ModuleManifest(name="m", version="1.0")
    assert m.resolver is False
    assert m.attachable is False


def test_manifest_resolver_and_attachable_roundtrip() -> None:
    m = ModuleManifest(name="m", version="1.0", resolver=True, attachable=True)
    restored = ModuleManifest.model_validate(m.model_dump())
    assert restored.resolver is True
    assert restored.attachable is True


def test_collections_spec_defaults() -> None:
    spec = CollectionsSpec(noun="calendar")
    assert spec.multi is False
    assert spec.providers == []


def test_manifest_collections_defaults_none() -> None:
    assert ModuleManifest(name="m", version="1.0").collections is None


def test_manifest_oauth_scopes_default_empty() -> None:
    assert ModuleManifest(name="m", version="1.0").oauth_scopes == {}


def test_manifest_oauth_scopes_roundtrip() -> None:
    scopes = {"google": ["https://www.googleapis.com/auth/calendar"]}
    m = ModuleManifest(name="calendar", version="0.6.0", oauth_scopes=scopes)
    restored = ModuleManifest.model_validate(m.model_dump())
    assert restored.oauth_scopes == scopes


def test_manifest_with_collections_roundtrips() -> None:
    m = ModuleManifest(
        name="calendar",
        version="0.5.0",
        collections=CollectionsSpec(noun="calendar", multi=True, providers=["google"]),
    )
    restored = ModuleManifest.model_validate(m.model_dump())
    assert restored == m
    assert restored.collections is not None
    assert restored.collections.noun == "calendar"
    assert restored.collections.multi is True
    assert restored.collections.providers == ["google"]


# ── writes_document: the live document pane's seam (#541, ADR-0100) ──────────


def test_tools_declare_no_writes_document_by_default() -> None:
    # The annotation is opt-in: the overwhelming majority of tools write no document, and
    # their calls must render exactly as before.
    assert ToolSpec(name="greet").writes_document is None


def test_writes_document_defaults_to_content_only() -> None:
    ann = WritesDocument(content_arg="content")
    assert ann.title_arg is None
    assert ann.target_arg is None
    assert ann.named_args() == ["content"]


def test_writes_document_named_args_lists_what_is_set() -> None:
    ann = WritesDocument(content_arg="content", title_arg="title", target_arg="path")
    assert ann.named_args() == ["content", "title", "path"]


def test_writes_document_requires_a_content_arg() -> None:
    with pytest.raises(ValidationError):
        WritesDocument(content_arg="")  # an empty name points at nothing


def test_writes_document_must_name_real_tool_arguments() -> None:
    # A typo'd arg would otherwise surface much later as a pane that never fills — fail at
    # manifest-build time instead.
    with pytest.raises(ValidationError, match="contents"):
        ToolSpec(
            name="create_doc",
            input_schema=_DOC_SCHEMA,
            writes_document=WritesDocument(content_arg="contents"),
        )
    with pytest.raises(ValidationError, match="heading"):
        ToolSpec(
            name="create_doc",
            input_schema=_DOC_SCHEMA,
            writes_document=WritesDocument(content_arg="content", title_arg="heading"),
        )


def test_writes_document_accepts_arguments_the_tool_declares() -> None:
    spec = ToolSpec(
        name="create_doc",
        input_schema=_DOC_SCHEMA,
        writes_document=WritesDocument(content_arg="content", title_arg="title", target_arg="path"),
    )
    assert spec.writes_document is not None
    assert spec.writes_document.content_arg == "content"


def test_writes_document_is_unchecked_when_the_tool_declares_no_properties() -> None:
    # input_schema is optional, so there is nothing to check against — annotate and move on
    # rather than reject a tool that simply didn't publish a schema.
    spec = ToolSpec(name="create_doc", writes_document=WritesDocument(content_arg="content"))
    assert spec.writes_document is not None


def test_manifest_with_writes_document_roundtrips() -> None:
    m = ModuleManifest(
        name="knowledge",
        version="0.22.0",
        tools=[
            ToolSpec(
                name="knowledge_create_doc",
                input_schema=_DOC_SCHEMA,
                writes_document=WritesDocument(
                    content_arg="content", title_arg="title", target_arg="path"
                ),
            ),
            ToolSpec(name="knowledge_search", input_schema={"type": "object"}),
        ],
    )
    restored = ModuleManifest.model_validate(m.model_dump())
    assert restored == m
    assert restored.tools[0].writes_document is not None
    assert restored.tools[0].writes_document.content_arg == "content"
    assert restored.tools[1].writes_document is None  # untouched by the annotation


def test_writes_document_is_ignored_by_a_reader_that_does_not_know_it() -> None:
    # Additive on the wire: a core predating the field parses the manifest fine and just drops
    # it, so a module can ship the annotation without waiting on the core that reads it.
    class OldToolSpec(BaseModel):
        """ToolSpec as it stood before ADR-0100."""

        name: str
        description: str = ""
        input_schema: dict[str, Any] = Field(default_factory=dict)

    payload = ToolSpec(
        name="create_doc",
        input_schema=_DOC_SCHEMA,
        writes_document=WritesDocument(content_arg="content"),
    ).model_dump()
    old = OldToolSpec.model_validate(payload)
    assert old.name == "create_doc"
    assert not hasattr(old, "writes_document")


def test_manifest_with_pages_roundtrips() -> None:
    m = ModuleManifest(
        name="files",
        version="1.0.0",
        pages=[
            PageSpec(id="browse", title="Files", archetype="browser", icon="folder", nav_order=10),
            PageSpec(id="board", title="Board", archetype="board"),
        ],
    )
    restored = ModuleManifest.model_validate(m.model_dump())
    assert restored == m
    assert [p.id for p in restored.pages] == ["browse", "board"]
    assert restored.pages[0].archetype == "browser"
    assert restored.pages[0].nav_order == 10
