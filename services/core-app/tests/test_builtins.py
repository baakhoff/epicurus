"""Tests for the core built-in `now` (ADR-0039) and `remember` (ADR-0045) tools."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from epicurus_core_app.agent.builtins import (
    MEMORY_SEARCH_SPEC,
    MEMORY_SEARCH_TOOL,
    NOW_SPEC,
    REMEMBER_SPEC,
    SET_CHAT_MODEL_SPEC,
    SET_CHAT_MODEL_TOOL,
    _resolve_model,
    make_memory_search_handler,
    make_now_handler,
    make_remember_handler,
    make_set_chat_model_handler,
)
from epicurus_core_app.llm.models import ModelInfo
from epicurus_core_app.memory.facts import UserFact
from epicurus_core_app.memory.memory import MemoryItem, SessionHit

# Built-in handlers take (arguments, tenant); `now` ignores the tenant.
Handler = Callable[[dict[str, Any], str], Awaitable[str]]


def _handler(
    tz: str = "Europe/Belgrade",
    calendar_tz: str | None = None,
    *,
    calendar_raises: bool = False,
) -> Handler:
    async def tz_provider() -> str:
        return tz

    async def calendar_tz_provider() -> str | None:
        if calendar_raises:
            raise RuntimeError("calendar down")
        return calendar_tz

    return make_now_handler(tz_provider, calendar_tz_provider)


def test_now_spec_shape() -> None:
    assert NOW_SPEC["function"]["name"] == "now"
    assert "timezone" in NOW_SPEC["function"]["parameters"]["properties"]


async def test_now_uses_configured_timezone() -> None:
    out = json.loads(await _handler(tz="Europe/Belgrade")({}, "local"))
    assert out["timezone"] == "Europe/Belgrade"
    assert "datetime" in out
    assert "utc" in out
    assert "weekday" in out


async def test_now_timezone_argument_overrides_setting() -> None:
    out = json.loads(await _handler(tz="UTC")({"timezone": "Asia/Tokyo"}, "local"))
    assert out["timezone"] == "Asia/Tokyo"


async def test_now_invalid_timezone_falls_back_to_utc() -> None:
    out = json.loads(await _handler(tz="Not/AZone")({}, "local"))
    assert out["timezone"] == "UTC"


async def test_now_reports_calendar_timezone_mismatch() -> None:
    out = json.loads(await _handler(tz="Europe/Belgrade", calendar_tz="Asia/Almaty")({}, "local"))
    assert out["calendar_timezone"] == "Asia/Almaty"
    assert "timezone_note" in out


async def test_now_omits_calendar_when_it_matches() -> None:
    handler = _handler(tz="Europe/Belgrade", calendar_tz="Europe/Belgrade")
    out = json.loads(await handler({}, "local"))
    assert "calendar_timezone" not in out
    assert "timezone_note" not in out


async def test_now_ignores_calendar_lookup_failure() -> None:
    out = json.loads(await _handler(tz="UTC", calendar_raises=True)({}, "local"))
    assert out["timezone"] == "UTC"
    assert "calendar_timezone" not in out


# ── remember (ADR-0045) ───────────────────────────────────────────────────────


class _FakeFactWriter:
    """Records remember_fact calls; ``duplicate`` makes the next save a no-op (returns None)."""

    def __init__(self, *, duplicate: bool = False, raises: bool = False) -> None:
        self._duplicate = duplicate
        self._raises = raises
        self.saved: list[tuple[str, str, str]] = []  # tenant, text, source

    async def remember_fact(
        self, *, tenant: str, text: str, source: str = "auto"
    ) -> UserFact | None:
        if self._raises:
            raise RuntimeError("qdrant down")
        self.saved.append((tenant, text, source))
        if self._duplicate:
            return None
        return UserFact(id="f1", text=text, source=source)


def test_remember_spec_shape() -> None:
    assert REMEMBER_SPEC["function"]["name"] == "remember"
    assert REMEMBER_SPEC["function"]["parameters"]["required"] == ["fact"]


async def test_remember_saves_the_fact_for_the_calling_tenant() -> None:
    writer = _FakeFactWriter()
    handler = make_remember_handler(writer)
    out = await handler({"fact": "Prefers metric units"}, "t1")
    assert writer.saved == [("t1", "Prefers metric units", "tool")]
    assert "Saved to memory" in out


async def test_remember_reports_a_duplicate_without_re_saving() -> None:
    writer = _FakeFactWriter(duplicate=True)
    out = await make_remember_handler(writer)({"fact": "Already known"}, "t1")
    assert "Already in memory" in out


async def test_remember_requires_a_fact() -> None:
    writer = _FakeFactWriter()
    out = await make_remember_handler(writer)({"fact": "   "}, "t1")
    assert out.startswith("error:")
    assert writer.saved == []


async def test_remember_surfaces_a_storage_failure_as_an_error() -> None:
    out = await make_remember_handler(_FakeFactWriter(raises=True))({"fact": "x"}, "t1")
    assert out.startswith("error:")


# ── memory_search (ADR-0089) ──────────────────────────────────────────────────


class _FakeMemorySearcher:
    """Fakes the memory facade's two search paths for the ``memory_search`` handler.

    ``facts_raise`` simulates a cold/failing embedder (the facts half); ``sessions_raise`` a
    DB hiccup (the sessions half). Records the tenant + limit each half was called with so a
    test can assert scoping and clamping.
    """

    def __init__(
        self,
        *,
        facts: list[MemoryItem] | None = None,
        sessions: list[SessionHit] | None = None,
        facts_raise: bool = False,
        sessions_raise: bool = False,
    ) -> None:
        self._facts = facts or []
        self._sessions = sessions or []
        self._facts_raise = facts_raise
        self._sessions_raise = sessions_raise
        self.facts_calls: list[tuple[str, int]] = []
        self.sessions_calls: list[tuple[str, int]] = []

    async def search_memory(
        self, *, tenant: str, query: str, limit: int = 20
    ) -> tuple[list[MemoryItem], int]:
        self.facts_calls.append((tenant, limit))
        if self._facts_raise:
            raise RuntimeError("embedder cold")
        return self._facts, len(self._facts)

    async def search_sessions(self, *, tenant: str, query: str, limit: int = 5) -> list[SessionHit]:
        self.sessions_calls.append((tenant, limit))
        if self._sessions_raise:
            raise RuntimeError("db down")
        return self._sessions


def _fact(text: str) -> MemoryItem:
    return MemoryItem(id="f", text=text, source="auto", score=0.8)


def _session(title: str, snippet: str, *, role: str = "assistant") -> SessionHit:
    return SessionHit(
        session_id="s1",
        title=title,
        role=role,
        snippet=snippet,
        created_at=datetime(2026, 7, 4, 9, 30, tzinfo=UTC),
    )


def test_memory_search_spec_shape() -> None:
    fn = MEMORY_SEARCH_SPEC["function"]
    assert fn["name"] == MEMORY_SEARCH_TOOL == "memory_search"
    assert fn["parameters"]["required"] == ["query"]
    assert fn["parameters"]["properties"]["scope"]["enum"] == ["facts", "sessions", "both"]


async def test_memory_search_returns_both_facts_and_sessions() -> None:
    searcher = _FakeMemorySearcher(
        facts=[_fact("Prefers restic for backups")],
        sessions=[_session("Backups", "we chose a nightly restic cron")],
    )
    out = await make_memory_search_handler(searcher)({"query": "backup"}, "t1")
    assert "Remembered facts:" in out
    assert "Prefers restic for backups" in out
    assert "From past conversations:" in out
    assert "2026-07-04" in out
    assert "Backups" in out
    # both halves searched the *calling* tenant (constraint #1)
    assert searcher.facts_calls == [("t1", 5)]
    assert searcher.sessions_calls == [("t1", 5)]


async def test_memory_search_scope_facts_skips_sessions() -> None:
    searcher = _FakeMemorySearcher(
        facts=[_fact("Lives in Belgrade")], sessions=[_session("x", "y")]
    )
    out = await make_memory_search_handler(searcher)({"query": "where", "scope": "facts"}, "t1")
    assert "Remembered facts:" in out
    assert "From past conversations:" not in out
    assert searcher.sessions_calls == []  # the sessions half was never touched


async def test_memory_search_scope_sessions_skips_facts() -> None:
    searcher = _FakeMemorySearcher(
        facts=[_fact("x")], sessions=[_session("Trip", "flights booked")]
    )
    out = await make_memory_search_handler(searcher)({"query": "trip", "scope": "sessions"}, "t1")
    assert "From past conversations:" in out
    assert "Remembered facts:" not in out
    assert searcher.facts_calls == []


async def test_memory_search_degrades_when_the_embedder_fails() -> None:
    # A cold embedder fails the facts half; the tool still returns the sessions half (no embed).
    searcher = _FakeMemorySearcher(sessions=[_session("Backups", "restic cron")], facts_raise=True)
    out = await make_memory_search_handler(searcher)({"query": "backup"}, "t1")
    assert not out.startswith("error:")
    assert "From past conversations:" in out
    assert "Remembered facts:" not in out


async def test_memory_search_reports_nothing_found() -> None:
    out = await make_memory_search_handler(_FakeMemorySearcher())({"query": "unicorn"}, "t1")
    assert "No remembered facts or past conversations matched" in out
    assert "unicorn" in out


async def test_memory_search_requires_a_query() -> None:
    searcher = _FakeMemorySearcher(facts=[_fact("x")])
    out = await make_memory_search_handler(searcher)({"query": "  "}, "t1")
    assert out.startswith("error:")
    assert searcher.facts_calls == []  # nothing searched on a blank query


async def test_memory_search_clamps_and_defaults_the_limit() -> None:
    searcher = _FakeMemorySearcher(facts=[_fact("x")])
    await make_memory_search_handler(searcher)({"query": "q", "limit": 99}, "t1")
    await make_memory_search_handler(searcher)({"query": "q", "limit": 0}, "t1")
    await make_memory_search_handler(searcher)({"query": "q", "limit": "junk"}, "t1")
    # 99 → 10 (cap), 0 → 1 (floor), junk → 5 (default)
    assert [limit for _tenant, limit in searcher.facts_calls] == [10, 1, 5]


async def test_memory_search_unknown_scope_falls_back_to_both() -> None:
    searcher = _FakeMemorySearcher(facts=[_fact("a")], sessions=[_session("b", "c")])
    out = await make_memory_search_handler(searcher)({"query": "q", "scope": "nonsense"}, "t1")
    assert "Remembered facts:" in out
    assert "From past conversations:" in out


# ── set_chat_model (#707) ─────────────────────────────────────────────────────


class _FakeLocalModels:
    def __init__(self, models: list[ModelInfo] | None = None, *, raises: bool = False) -> None:
        self._models = models or []
        self._raises = raises
        self.tenants: list[str | None] = []

    async def models(self, tenant_id: str | None = None) -> list[ModelInfo]:
        self.tenants.append(tenant_id)
        if self._raises:
            raise RuntimeError("ollama down")
        return self._models


class _FakeSavedModels:
    def __init__(self, models: list[str] | None = None, *, raises: bool = False) -> None:
        self._models = models or []
        self._raises = raises
        self.tenants: list[str] = []

    async def list(self, tenant: str) -> list[str]:
        self.tenants.append(tenant)
        if self._raises:
            raise RuntimeError("db down")
        return self._models


class _FakeSessionModels:
    def __init__(self, *, raises: bool = False) -> None:
        self._raises = raises
        self.set_calls: list[tuple[str, str, str]] = []  # tenant, session_id, model

    async def set(self, *, tenant: str, session_id: str, model: str) -> None:
        if self._raises:
            raise RuntimeError("store down")
        self.set_calls.append((tenant, session_id, model))


def _model(name: str, *, hidden: bool = False) -> ModelInfo:
    return ModelInfo(name=name, hidden=hidden)


def test_set_chat_model_spec_shape() -> None:
    fn = SET_CHAT_MODEL_SPEC["function"]
    assert fn["name"] == SET_CHAT_MODEL_TOOL == "set_chat_model"
    assert fn["parameters"]["required"] == ["model"]


# ── _resolve_model ──────────────────────────────────────────────────────────


def test_resolve_model_exact_match() -> None:
    assert _resolve_model("qwen2.5:7b", ["qwen2.5:7b", "llama3.3"]) == "qwen2.5:7b"


def test_resolve_model_case_insensitive_exact_match() -> None:
    assert _resolve_model("QWEN2.5:7B", ["qwen2.5:7b"]) == "qwen2.5:7b"


def test_resolve_model_unique_substring_match() -> None:
    assert _resolve_model("grok", ["grok/grok-4.5-latest", "claude/opus"]) == "grok/grok-4.5-latest"


def test_resolve_model_ambiguous_substring_refuses_to_guess() -> None:
    available = ["grok/grok-4.5-latest", "grok/grok-4-fast"]
    assert _resolve_model("grok", available) is None


def test_resolve_model_unknown_name_returns_none() -> None:
    assert _resolve_model("bard", ["qwen2.5:7b"]) is None


def test_resolve_model_blank_query_returns_none() -> None:
    assert _resolve_model("   ", ["qwen2.5:7b"]) is None


# ── make_set_chat_model_handler ──────────────────────────────────────────────


async def test_set_chat_model_switches_to_a_local_model() -> None:
    local = _FakeLocalModels([_model("qwen2.5:7b"), _model("llama3.3")])
    saved = _FakeSavedModels()
    sessions = _FakeSessionModels()
    handler = make_set_chat_model_handler(local, saved, sessions)
    out = await handler({"model": "qwen2.5:7b"}, "t1", "s1")
    assert sessions.set_calls == [("t1", "s1", "qwen2.5:7b")]
    assert "qwen2.5:7b" in out
    assert not out.startswith("error:")


async def test_set_chat_model_switches_to_a_saved_hosted_model_by_partial_name() -> None:
    local = _FakeLocalModels([_model("qwen2.5:7b")])
    saved = _FakeSavedModels(["grok/grok-4.5-latest"])
    sessions = _FakeSessionModels()
    out = await make_set_chat_model_handler(local, saved, sessions)({"model": "grok"}, "t1", "s1")
    assert sessions.set_calls == [("t1", "s1", "grok/grok-4.5-latest")]
    assert "grok/grok-4.5-latest" in out


async def test_set_chat_model_excludes_hidden_local_models() -> None:
    # Hidden from chat pickers too (ADR-0029) — resolving against "what the picker offers"
    # must not let a hidden model be switched to by name.
    local = _FakeLocalModels([_model("qwen2.5:7b", hidden=True)])
    saved = _FakeSavedModels()
    sessions = _FakeSessionModels()
    out = await make_set_chat_model_handler(local, saved, sessions)(
        {"model": "qwen2.5:7b"}, "t1", "s1"
    )
    assert out.startswith("error:")
    assert sessions.set_calls == []


async def test_set_chat_model_unknown_name_changes_nothing_and_lists_available() -> None:
    local = _FakeLocalModels([_model("qwen2.5:7b")])
    saved = _FakeSavedModels(["grok/grok-4.5-latest"])
    sessions = _FakeSessionModels()
    out = await make_set_chat_model_handler(local, saved, sessions)({"model": "bard"}, "t1", "s1")
    assert out.startswith("error:")
    assert "qwen2.5:7b" in out
    assert "grok/grok-4.5-latest" in out
    assert sessions.set_calls == []


async def test_set_chat_model_ambiguous_name_changes_nothing_and_lists_available() -> None:
    local = _FakeLocalModels([])
    saved = _FakeSavedModels(["grok/grok-4.5-latest", "grok/grok-4-fast"])
    sessions = _FakeSessionModels()
    out = await make_set_chat_model_handler(local, saved, sessions)({"model": "grok"}, "t1", "s1")
    assert out.startswith("error:")
    assert sessions.set_calls == []


async def test_set_chat_model_requires_a_model_argument() -> None:
    handler = make_set_chat_model_handler(
        _FakeLocalModels(), _FakeSavedModels(), _FakeSessionModels()
    )
    out = await handler({"model": "  "}, "t1", "s1")
    assert out.startswith("error:")


async def test_set_chat_model_requires_an_active_session() -> None:
    # No session to persist the choice into (e.g. some headless path) — errors plainly
    # rather than silently doing nothing.
    local = _FakeLocalModels([_model("qwen2.5:7b")])
    sessions = _FakeSessionModels()
    out = await make_set_chat_model_handler(local, _FakeSavedModels(), sessions)(
        {"model": "qwen2.5:7b"}, "t1", None
    )
    assert out.startswith("error:")
    assert sessions.set_calls == []


async def test_set_chat_model_reports_when_nothing_is_available() -> None:
    handler = make_set_chat_model_handler(
        _FakeLocalModels(), _FakeSavedModels(), _FakeSessionModels()
    )
    out = await handler({"model": "anything"}, "t1", "s1")
    assert out.startswith("error:")


async def test_set_chat_model_degrades_when_the_local_catalog_fails() -> None:
    # A cold/unreachable Ollama must not crash the tool — the saved-hosted half still works.
    local = _FakeLocalModels(raises=True)
    saved = _FakeSavedModels(["grok/grok-4.5-latest"])
    sessions = _FakeSessionModels()
    out = await make_set_chat_model_handler(local, saved, sessions)({"model": "grok"}, "t1", "s1")
    assert not out.startswith("error:")
    assert sessions.set_calls == [("t1", "s1", "grok/grok-4.5-latest")]


async def test_set_chat_model_degrades_when_the_saved_list_fails() -> None:
    local = _FakeLocalModels([_model("qwen2.5:7b")])
    saved = _FakeSavedModels(raises=True)
    sessions = _FakeSessionModels()
    out = await make_set_chat_model_handler(local, saved, sessions)(
        {"model": "qwen2.5:7b"}, "t1", "s1"
    )
    assert not out.startswith("error:")
    assert sessions.set_calls == [("t1", "s1", "qwen2.5:7b")]


async def test_set_chat_model_surfaces_a_persist_failure() -> None:
    local = _FakeLocalModels([_model("qwen2.5:7b")])
    sessions = _FakeSessionModels(raises=True)
    out = await make_set_chat_model_handler(local, _FakeSavedModels(), sessions)(
        {"model": "qwen2.5:7b"}, "t1", "s1"
    )
    assert out.startswith("error:")


async def test_set_chat_model_scopes_the_catalog_lookups_to_the_calling_tenant() -> None:
    local = _FakeLocalModels([_model("qwen2.5:7b")])
    saved = _FakeSavedModels(["grok/grok-4.5-latest"])
    await make_set_chat_model_handler(local, saved, _FakeSessionModels())(
        {"model": "qwen2.5:7b"}, "tenant-9", "s1"
    )
    assert local.tenants == ["tenant-9"]
    assert saved.tenants == ["tenant-9"]
