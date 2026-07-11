"""Unit tests for the standing profile store and synthesizer (#527, ADR-0094)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.llm.models import ChatMessage, ChatResult
from epicurus_core_app.llm.power import GatewayPausedError
from epicurus_core_app.memory.facts import UserFact
from epicurus_core_app.memory.profile import (
    SOURCE_AUTO,
    SOURCE_EDITED,
    ProfileSynthesizer,
    StandingProfileStore,
)


async def _fresh_store(max_versions: int = 5) -> tuple[StandingProfileStore, AsyncEngine]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = StandingProfileStore(engine, max_versions=max_versions)
    await store.init()
    return store, engine


# ── StandingProfileStore ──────────────────────────────────────────────────────


async def test_save_and_latest_round_trip() -> None:
    store, _ = await _fresh_store()
    saved = await store.save(tenant="t1", content="The user prefers metric units.")
    latest = await store.latest(tenant="t1")
    assert latest is not None
    assert latest.id == saved.id
    assert latest.content == "The user prefers metric units."
    assert latest.source == SOURCE_AUTO


async def test_latest_is_the_newest_version() -> None:
    store, _ = await _fresh_store()
    await store.save(tenant="t1", content="old")
    await store.save(tenant="t1", content="new")
    latest = await store.latest(tenant="t1")
    assert latest is not None and latest.content == "new"


async def test_latest_is_none_when_empty() -> None:
    store, _ = await _fresh_store()
    assert await store.latest(tenant="t1") is None


async def test_versions_are_newest_first() -> None:
    store, _ = await _fresh_store()
    for text in ["v1", "v2", "v3"]:
        await store.save(tenant="t1", content=text)
    assert [v.content for v in await store.versions(tenant="t1")] == ["v3", "v2", "v1"]


async def test_save_prunes_to_max_versions() -> None:
    store, _ = await _fresh_store(max_versions=2)
    for text in ["v1", "v2", "v3", "v4"]:
        await store.save(tenant="t1", content=text)
    # only the two newest survive the per-write prune
    assert [v.content for v in await store.versions(tenant="t1", limit=10)] == ["v4", "v3"]


async def test_store_is_tenant_scoped() -> None:
    store, _ = await _fresh_store()
    await store.save(tenant="t1", content="t1 profile")
    await store.save(tenant="t2", content="t2 profile")
    assert (await store.latest(tenant="t1")).content == "t1 profile"  # type: ignore[union-attr]
    assert (await store.latest(tenant="t2")).content == "t2 profile"  # type: ignore[union-attr]


async def test_clear_removes_all_versions_for_the_tenant_only() -> None:
    store, _ = await _fresh_store()
    await store.save(tenant="t1", content="a")
    await store.save(tenant="t1", content="b")
    await store.save(tenant="t2", content="keep")
    removed = await store.clear(tenant="t1")
    assert removed == 2
    assert await store.latest(tenant="t1") is None
    assert (await store.latest(tenant="t2")).content == "keep"  # type: ignore[union-attr]


# ── ProfileSynthesizer ────────────────────────────────────────────────────────


class _FakeChat:
    """A gateway stand-in returning a canned reply (or raising, to simulate a paused gateway)."""

    def __init__(self, content: str = "", *, paused: bool = False) -> None:
        self._content = content
        self._paused = paused
        self.calls: list[tuple[str | None, str | None]] = []  # (model, tenant_id)

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: list[dict[str, object]] | None = None,
        tenant_id: str | None = None,
    ) -> ChatResult:
        self.calls.append((model, tenant_id))
        if self._paused:
            raise GatewayPausedError("asleep")
        return ChatResult(model="m", content=self._content)


class _FakeFacts:
    """A fact source keyed by tenant."""

    def __init__(self, by_tenant: dict[str, list[str]]) -> None:
        self._by_tenant = by_tenant

    async def list_facts(self, *, tenant: str, limit: int = 200) -> list[UserFact]:
        return [
            UserFact(id=f"{tenant}-{i}", text=t)
            for i, t in enumerate(self._by_tenant.get(tenant, []))
        ]


def _synth(
    store: StandingProfileStore,
    *,
    content: str = "The user lives in Belgrade and prefers concise answers.",
    facts: dict[str, list[str]] | None = None,
    tenants: list[str] | None = None,
    paused: bool = False,
    model: str | None = None,
) -> tuple[ProfileSynthesizer, _FakeChat]:
    chat = _FakeChat(content, paused=paused)
    facts_src = _FakeFacts(facts or {"local": ["Lives in Belgrade", "Prefers concise answers"]})

    async def tenant_provider() -> list[str]:
        return tenants if tenants is not None else ["local"]

    return ProfileSynthesizer(chat, facts_src, store, tenants=tenant_provider, model=model), chat


async def test_synthesize_writes_an_auto_profile_from_facts() -> None:
    store, _ = await _fresh_store()
    synth, chat = _synth(store)
    saved = await synth.synthesize(tenant="local")
    assert saved is not None
    assert saved.source == SOURCE_AUTO
    assert "Belgrade" in saved.content
    assert chat.calls == [
        (None, "local")
    ]  # synthesis metered under the calling tenant (const. #8/#1)
    assert (await store.latest(tenant="local")).content == saved.content  # type: ignore[union-attr]


async def test_synthesize_passes_a_dedicated_model_when_configured() -> None:
    store, _ = await _fresh_store()
    synth, chat = _synth(store, model="tiny:1b")
    await synth.synthesize(tenant="local")
    assert chat.calls == [("tiny:1b", "local")]


async def test_synthesize_skips_when_the_tenant_has_no_facts() -> None:
    store, _ = await _fresh_store()
    synth, chat = _synth(store, facts={"local": []})
    assert await synth.synthesize(tenant="local") is None
    assert chat.calls == []  # no LLM call when there's nothing to distil
    assert await store.latest(tenant="local") is None


async def test_synthesize_preserves_an_operator_pinned_edit() -> None:
    # The corrections-survive-re-synthesis rule (#527): an edited profile is never clobbered.
    store, _ = await _fresh_store()
    await store.save(tenant="local", content="Operator's own words.", source=SOURCE_EDITED)
    synth, chat = _synth(store)
    assert await synth.synthesize(tenant="local") is None
    assert chat.calls == []  # didn't even call the model — the edit is pinned
    latest = await store.latest(tenant="local")
    assert latest is not None and latest.content == "Operator's own words."
    assert latest.source == SOURCE_EDITED


async def test_synthesize_keeps_previous_profile_on_empty_model_output() -> None:
    store, _ = await _fresh_store()
    await store.save(tenant="local", content="prior auto profile", source=SOURCE_AUTO)
    synth, _ = _synth(store, content="   ")  # model declines / returns whitespace
    assert await synth.synthesize(tenant="local") is None
    latest = await store.latest(tenant="local")
    assert latest is not None and latest.content == "prior auto profile"  # not wiped


async def test_run_fans_out_over_tenants_and_counts_written() -> None:
    store, _ = await _fresh_store()
    synth, _ = _synth(
        store,
        facts={"t1": ["a fact"], "t2": [], "t3": ["another"]},  # t2 has no facts → skipped
        tenants=["t1", "t2", "t3"],
    )
    assert await synth.run() == 2
    assert await store.latest(tenant="t1") is not None
    assert await store.latest(tenant="t2") is None
    assert await store.latest(tenant="t3") is not None


async def test_run_stops_when_the_gateway_is_paused() -> None:
    store, _ = await _fresh_store()
    synth, _ = _synth(store, facts={"t1": ["x"], "t2": ["y"]}, tenants=["t1", "t2"], paused=True)
    # A paused gateway stops the batch (the model is asleep) — nothing written.
    assert await synth.run() == 0
    assert await store.latest(tenant="t1") is None


async def test_run_skips_a_failing_tenant_and_continues() -> None:
    store, _ = await _fresh_store()

    class _FlakyFacts(_FakeFacts):
        async def list_facts(self, *, tenant: str, limit: int = 200) -> list[UserFact]:
            if tenant == "boom":
                raise RuntimeError("qdrant down")
            return await super().list_facts(tenant=tenant, limit=limit)

    chat = _FakeChat("A profile.")

    async def tenants() -> list[str]:
        return ["boom", "ok"]

    synth = ProfileSynthesizer(chat, _FlakyFacts({"ok": ["good fact"]}), store, tenants=tenants)
    assert await synth.run() == 1  # 'boom' errored and was skipped; 'ok' still synthesized
    assert await store.latest(tenant="ok") is not None
