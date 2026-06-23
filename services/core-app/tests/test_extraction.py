"""Unit tests for background fact extraction (ADR-0045) — gateway + store are faked."""

from __future__ import annotations

import uuid

from epicurus_core_app.llm.models import ChatMessage, ChatResult
from epicurus_core_app.llm.power import GatewayPausedError
from epicurus_core_app.memory.extraction import FactExtractor
from epicurus_core_app.memory.facts import SOURCE_AUTO, UserFact


class _FakeGateway:
    """Returns a canned completion; records the messages it was asked to complete."""

    def __init__(self, content: str = "[]", *, paused: bool = False) -> None:
        self._content = content
        self._paused = paused
        self.calls: list[list[ChatMessage]] = []

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: object = None,
        tenant_id: str | None = None,
    ) -> ChatResult:
        if self._paused:
            raise GatewayPausedError("paused")
        self.calls.append(list(messages))
        return ChatResult(model="m", content=self._content)


class _FakeFacts:
    """Records saves; ``dupes`` (by text) makes save a no-op returning None."""

    def __init__(self, *, dupes: set[str] | None = None) -> None:
        self._dupes = dupes or set()
        self.saved: list[tuple[str, str, str]] = []  # tenant, text, source

    async def save(self, *, tenant: str, text: str, source: str = SOURCE_AUTO) -> UserFact | None:
        if text in self._dupes:
            return None
        self.saved.append((tenant, text, source))
        return UserFact(id=str(uuid.uuid4()), text=text, source=source)


def _extractor(content: str, **kw: object) -> tuple[FactExtractor, _FakeGateway, _FakeFacts]:
    gw = _FakeGateway(content, **kw)  # type: ignore[arg-type]
    facts = _FakeFacts()
    return FactExtractor(gw, facts), gw, facts  # type: ignore[arg-type]


async def test_extracts_and_saves_each_fact_as_auto() -> None:
    extractor, _, facts = _extractor('["Lives in Belgrade", "Prefers metric units"]')
    saved = await extractor.extract(
        tenant="t1", user_text="I live in Belgrade and like metric", assistant_text="Noted!"
    )
    assert [f.text for f in saved] == ["Lives in Belgrade", "Prefers metric units"]
    assert facts.saved == [
        ("t1", "Lives in Belgrade", SOURCE_AUTO),
        ("t1", "Prefers metric units", SOURCE_AUTO),
    ]


async def test_tolerates_code_fenced_json() -> None:
    extractor, _, _ = _extractor('Sure!\n```json\n["Name is Sam"]\n```')
    saved = await extractor.extract(tenant="t1", user_text="I'm Sam", assistant_text="Hi Sam")
    assert [f.text for f in saved] == ["Name is Sam"]


async def test_empty_array_saves_nothing() -> None:
    extractor, _, facts = _extractor("[]")
    saved = await extractor.extract(tenant="t1", user_text="what time is it?", assistant_text="3pm")
    assert saved == []
    assert facts.saved == []


async def test_non_json_reply_saves_nothing() -> None:
    extractor, _, facts = _extractor("I could not find anything to remember.")
    saved = await extractor.extract(tenant="t1", user_text="hello", assistant_text="hi")
    assert saved == []
    assert facts.saved == []


async def test_blank_user_text_skips_the_call() -> None:
    extractor, gw, _ = _extractor('["should not be reached"]')
    saved = await extractor.extract(tenant="t1", user_text="   ", assistant_text="hi")
    assert saved == []
    assert gw.calls == []  # the gateway is never asked


async def test_paused_gateway_is_silent() -> None:
    extractor, _, facts = _extractor('["anything"]', paused=True)
    saved = await extractor.extract(tenant="t1", user_text="remember this", assistant_text="ok")
    assert saved == []
    assert facts.saved == []


async def test_caps_the_number_of_facts() -> None:
    many = "[" + ", ".join(f'"fact {i}"' for i in range(20)) + "]"
    extractor, _, facts = _extractor(many)
    saved = await extractor.extract(tenant="t1", user_text="lots", assistant_text="ok")
    assert len(saved) <= 6  # _MAX_FACTS
    assert len(facts.saved) == len(saved)


async def test_deduped_facts_are_not_returned() -> None:
    gw = _FakeGateway('["already known", "brand new"]')
    facts = _FakeFacts(dupes={"already known"})
    extractor = FactExtractor(gw, facts)  # type: ignore[arg-type]
    saved = await extractor.extract(tenant="t1", user_text="x", assistant_text="y")
    assert [f.text for f in saved] == ["brand new"]
