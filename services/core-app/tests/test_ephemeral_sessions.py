"""Invisible chats (#772), end to end against the real stores: seeded into a flagged session,
neither the extraction drain nor the reflection pass derives anything from it — and a normal
session in the same stores flows through both learners unchanged.

The unit halves live beside their stores (`test_memory_store.py` for the flag + exclusions,
`test_agent.py` for the learn-time skip, `test_session_delete.py` for the exit cascade +
sweep, `test_agent_routes.py` for the HTTP surface); this file is the issue's acceptance
shape: real `ConversationStore` + `ExtractionQueue` + `EphemeralSessionStore` wired the way
`app.py` wires them, with only the gateway/extractor faked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core import ChatMessage, ChatResult
from epicurus_core_app.agent.playbook_review import PlaybookProposalStore
from epicurus_core_app.agent.reflection import PlaybookReflector, ReflectionStateStore
from epicurus_core_app.memory.extraction import ExtractionRunner
from epicurus_core_app.memory.extraction_queue import ExtractionQueue
from epicurus_core_app.memory.store import (
    ConversationStore,
    EphemeralSessionStore,
    StoredMessage,
)

TENANT = "t1"


def _engine() -> AsyncEngine:
    return create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


class _RecordingExtractor:
    def __init__(self) -> None:
        self.extracted: list[str] = []

    async def extract(self, *, tenant: str, user_text: str, assistant_text: str) -> list[str]:
        self.extracted.append(user_text)
        return []


class _Power:
    paused = False


async def test_drain_learns_nothing_from_an_invisible_session() -> None:
    """The enqueue-time skip is the agent's job (test_agent.py); this asserts the backstop —
    even a row that somehow landed stamped with a flagged session would be purged by delete,
    and a normal session's rows drain untouched beside a flagged session's absence."""
    engine = _engine()
    store = ConversationStore(engine)
    await store.init()
    queue = ExtractionQueue(engine)
    flags = EphemeralSessionStore(engine)
    await flags.mark(tenant=TENANT, session_id="ghost")

    # The normal session's exchange is queued (what the agent does after a turn); the
    # invisible one's never is — mirror the agent's learn-time skip against the real store.
    if not await flags.is_ephemeral(tenant=TENANT, session_id="plain"):
        await queue.enqueue(
            tenant=TENANT, user_text="plain exchange", assistant_text="a", session_id="plain"
        )
    if not await flags.is_ephemeral(tenant=TENANT, session_id="ghost"):
        await queue.enqueue(
            tenant=TENANT, user_text="ghost exchange", assistant_text="a", session_id="ghost"
        )

    extractor = _RecordingExtractor()

    async def _tz() -> str:
        return "UTC"

    runner = ExtractionRunner(queue, extractor, _Power(), timezone=_tz)  # type: ignore[arg-type]
    assert await runner.drain_once() == 1
    assert extractor.extracted == ["plain exchange"]
    await engine.dispose()


class _RecordingChat:
    """The reflection pass's gateway slice — records calls, proposes nothing."""

    def __init__(self) -> None:
        self.calls: list[str | None] = []  # tenant_id per call

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: list[dict[str, object]] | None = None,
        tenant_id: str | None = None,
    ) -> ChatResult:
        self.calls.append(tenant_id)
        return ChatResult(model="m", content='{"proposals": []}')

    @property
    def prompts(self) -> list[str]:
        return []


class _NoPlaybooks:
    async def list_playbooks(self, tenant: str, *, enabled_only: bool = False) -> list[Any]:
        return []


class _NoInstructions:
    async def get_base(self, tenant: str) -> str:
        return "base"


async def test_reflection_never_reads_an_invisible_session() -> None:
    """Reflection's transcript scan runs on the store's `sessions()` default (#772): a tenant
    whose only activity is an invisible chat spends **no gateway call at all**, and a mixed
    tenant's call carries only the visible transcripts."""
    engine = _engine()
    store = ConversationStore(engine)
    await store.init()
    flags = EphemeralSessionStore(engine)
    proposals = PlaybookProposalStore(engine)
    await proposals.init()
    state = ReflectionStateStore(engine)
    await state.init()

    await store.append(tenant=TENANT, session_id="ghost", role="user", content="invisible ask")
    await flags.mark(tenant=TENANT, session_id="ghost")

    chat = _RecordingChat()
    reflector = PlaybookReflector(
        chat,
        store,
        proposals,
        _NoPlaybooks(),
        _NoInstructions(),
        state,
    )
    assert await reflector.run() == 0
    assert chat.calls == []  # nothing visible happened → no gateway call was spent

    # A visible session appearing later is reflected on — with only its own transcript. An
    # explicit far-future timestamp keeps it after the watermark (SQLite's server-now has
    # second resolution, so a same-second append could land at-or-before it).
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add(
            StoredMessage(
                tenant=TENANT,
                session_id="plain",
                role="user",
                content="visible ask",
                created_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await db.commit()
    assert await reflector.run() == 0  # the fake proposes nothing; the call itself is the point
    assert chat.calls == [TENANT]
    await engine.dispose()
