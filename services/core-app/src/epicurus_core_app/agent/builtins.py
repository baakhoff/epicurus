"""Core built-in agent tools (ADR-0039).

Tools the core provides directly, alongside the module tools discovered over MCP. Unlike
module tools they are dispatched in-process (no HTTP), and they receive the calling tenant
so a built-in can read or write tenant-scoped state. ``now`` reports the current date/time
(without it the model guesses the date from its training cutoff); ``remember`` saves a
durable fact about the user to long-term memory (ADR-0045).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from epicurus_core import get_logger
from epicurus_core_app.memory.facts import SOURCE_TOOL, UserFact
from epicurus_core_app.memory.memory import MemoryItem, SessionHit

log = get_logger("epicurus_core_app.agent.builtins")

# The OpenAI-style function spec the gateway sends to the model.
NOW_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "now",
        "description": (
            "Get the current date and time. Call this whenever a request involves the "
            "current date or a relative time (today, tomorrow, next week, 'at 19:00', "
            "'in 2 hours') so dates and times are correct. Returns the time in the "
            "operator's configured timezone; if a connected calendar uses a different "
            "timezone that is reported too. Pass `timezone` to get the time in a specific "
            "IANA zone instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": (
                        "Optional IANA timezone name (e.g. 'Europe/Belgrade') to report "
                        "instead of the operator's configured timezone."
                    ),
                }
            },
        },
    },
}

#: Returns the operator's configured IANA timezone.
TimezoneProvider = Callable[[], Awaitable[str]]
#: Returns the connected calendar's IANA timezone, or ``None`` (best-effort).
CalendarTzProvider = Callable[[], Awaitable[str | None]]

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_UTC = ZoneInfo("UTC")


def _resolve_zone(name: str) -> tuple[ZoneInfo, str]:
    """Return ``(ZoneInfo, effective_name)``, falling back to UTC on an unknown zone."""
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown timezone; falling back to UTC", timezone=name)
        return _UTC, "UTC"


def make_now_handler(
    tz_provider: TimezoneProvider,
    calendar_tz_provider: CalendarTzProvider,
) -> Callable[[dict[str, Any], str], Awaitable[str]]:
    """Build the ``now`` handler closed over its timezone + calendar-tz sources.

    The handler reports the current time in the operator's configured timezone (or an
    explicit ``timezone`` argument). It also reports the connected calendar's timezone and
    a note when it differs from the configured one, so the agent creates events at the
    intended local time. The calendar lookup is best-effort — any failure is omitted, never
    raised. ``now`` is tenant-agnostic, so the tenant argument is accepted and ignored.
    """

    async def handler(arguments: dict[str, Any], _tenant: str) -> str:
        configured = await tz_provider()
        requested = arguments.get("timezone")
        wanted = requested if isinstance(requested, str) and requested.strip() else configured
        zone, zone_name = _resolve_zone(wanted)
        now = datetime.now(tz=zone)
        payload: dict[str, Any] = {
            "datetime": now.isoformat(timespec="seconds"),
            "timezone": zone_name,
            "utc": now.astimezone(_UTC).isoformat(timespec="seconds"),
            "weekday": _WEEKDAYS[now.weekday()],
        }
        # Best-effort: surface the calendar's tz when it differs, so the model knows which
        # zone new events land in. A calendar hiccup must never break `now`.
        try:
            calendar_tz = await calendar_tz_provider()
        except Exception as exc:
            log.warning("calendar timezone lookup failed", error=str(exc))
            calendar_tz = None
        if calendar_tz and calendar_tz != zone_name:
            payload["calendar_timezone"] = calendar_tz
            payload["timezone_note"] = (
                f"The connected calendar uses {calendar_tz}, which differs from the "
                f"configured timezone {zone_name}. Create calendar events in {calendar_tz} "
                "unless the user says otherwise."
            )
        return json.dumps(payload)

    return handler


# ── remember ──────────────────────────────────────────────────────────────────

REMEMBER_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "remember",
        "description": (
            "Save a durable fact about the user to long-term memory so you recall it in "
            "future conversations. Call this when the user asks you to remember something, "
            "or when you learn a stable detail or preference about them — their name, where "
            "they live, how they like you to respond, an ongoing project. Keep each fact a "
            "short standalone statement. Do not save one-off task details, secrets, or "
            "passwords."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": (
                        "The fact to remember, as a short standalone statement, e.g. "
                        "'Prefers responses in metric units.'"
                    ),
                }
            },
            "required": ["fact"],
        },
    },
}


class FactWriter(Protocol):
    """The slice of the memory facade the ``remember`` tool needs (eases faking in tests)."""

    async def remember_fact(
        self, *, tenant: str, text: str, source: str = ...
    ) -> UserFact | None: ...


def make_remember_handler(
    memory: FactWriter,
) -> Callable[[dict[str, Any], str], Awaitable[str]]:
    """Build the ``remember`` handler closed over the memory facade (ADR-0045).

    Saves the fact to the calling tenant's user-fact memory. A near-duplicate of an existing
    fact is a no-op (the store dedups); any failure is reported to the model as an ``error:``
    string rather than raised, so a memory hiccup never breaks the turn.
    """

    async def handler(arguments: dict[str, Any], tenant: str) -> str:
        fact = str(arguments.get("fact") or "").strip()
        if not fact:
            return "error: a `fact` to remember is required."
        try:
            saved = await memory.remember_fact(tenant=tenant, text=fact, source=SOURCE_TOOL)
        except Exception as exc:  # surface to the model, never crash the turn
            log.warning("remember tool save failed", error=str(exc))
            return f"error: could not save that to memory: {exc}"
        if saved is None:
            return "Already in memory — nothing new to add."
        return f"Saved to memory: {saved.text}"

    return handler


# ── memory_search (ADR-0089) ────────────────────────────────────────────────

#: The tool name for deliberate recall over the fact store and past conversations.
MEMORY_SEARCH_TOOL = "memory_search"

# Result discipline: cap what the tool returns so a broad query can't flood the context.
_MEMORY_SEARCH_DEFAULT_LIMIT = 5
_MEMORY_SEARCH_MAX_LIMIT = 10
_MEMORY_SNIPPET_CAP = 240
_MEMORY_SEARCH_SCOPES = ("facts", "sessions", "both")

MEMORY_SEARCH_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": MEMORY_SEARCH_TOOL,
        "description": (
            "Search your long-term memory for something from the past: durable facts you "
            "remember about the user, and excerpts of earlier conversations with them. Call "
            "this when the user refers to something discussed or decided before — 'what did we "
            "settle on for the backup strategy?', 'the library I mentioned last week' — and it "
            "isn't already in this conversation. You already receive ambient memory "
            "automatically each turn; use this to deliberately dig for specifics you weren't "
            "handed. Returns the most relevant facts and past-conversation snippets, newest "
            "first for conversations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What to look for, in a few words — e.g. 'backup strategy', "
                        "'the user's sister's name', 'database we chose'."
                    ),
                },
                "scope": {
                    "type": "string",
                    "enum": list(_MEMORY_SEARCH_SCOPES),
                    "description": (
                        "Which memory to search: 'facts' (durable facts about the user), "
                        "'sessions' (past conversations), or 'both' (default)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum results per source (1-10, default 5). Keep it small unless "
                        "you truly need more."
                    ),
                },
            },
            "required": ["query"],
        },
    },
}


class MemorySearcher(Protocol):
    """The slice of the memory facade ``memory_search`` needs (eases faking in tests)."""

    async def search_memory(
        self, *, tenant: str, query: str, limit: int = ...
    ) -> tuple[list[MemoryItem], int]: ...

    async def search_sessions(
        self, *, tenant: str, query: str, limit: int = ...
    ) -> list[SessionHit]: ...


def _coerce_limit(raw: Any) -> int:
    """Clamp the model-supplied ``limit`` to ``[1, _MEMORY_SEARCH_MAX_LIMIT]`` (default on junk)."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _MEMORY_SEARCH_DEFAULT_LIMIT
    return max(1, min(value, _MEMORY_SEARCH_MAX_LIMIT))


def _format_session_hit(hit: SessionHit) -> str:
    """One compact past-conversation line: date + conversation title + role + snippet."""
    when = hit.created_at.date().isoformat() if hit.created_at else "unknown date"
    title = hit.title.strip() or "(untitled)"
    snippet = " ".join(hit.snippet.split())[:_MEMORY_SNIPPET_CAP]
    return f'- [{when} · "{title}"] {hit.role}: {snippet}'


def make_memory_search_handler(
    memory: MemorySearcher,
) -> Callable[[dict[str, Any], str], Awaitable[str]]:
    """Build the ``memory_search`` handler closed over the memory facade (ADR-0089).

    Deliberate recall for the **calling tenant** — built-in handlers receive the tenant precisely
    so a cross-session search never leaks another tenant's memory (constraint #1). Best-effort
    like the rest of memory: the facts half embeds the query (through the gateway, constraint #8),
    so a cold or failing embedder degrades to just the sessions half (a Postgres text search that
    needs no embedding) rather than failing the tool call hard; either half's error is caught and
    that half is simply omitted. Results are capped and compact — never a raw session dump.
    """

    async def handler(arguments: dict[str, Any], tenant: str) -> str:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return "error: a `query` to search for is required."
        scope = str(arguments.get("scope") or "both").strip().lower()
        if scope not in _MEMORY_SEARCH_SCOPES:
            scope = "both"
        limit = _coerce_limit(arguments.get("limit"))
        sections: list[str] = []

        if scope in ("facts", "both"):
            try:
                items, _ = await memory.search_memory(tenant=tenant, query=query, limit=limit)
            except Exception as exc:  # a cold embedder must not fail the call — degrade to text
                log.warning("memory_search facts lookup failed; degrading", error=str(exc))
                items = []
            if items:
                lines = "\n".join(f"- {item.text}" for item in items)
                sections.append(f"Remembered facts:\n{lines}")

        if scope in ("sessions", "both"):
            try:
                hits = await memory.search_sessions(tenant=tenant, query=query, limit=limit)
            except Exception as exc:  # a DB hiccup omits this half rather than crashing the turn
                log.warning("memory_search sessions lookup failed; degrading", error=str(exc))
                hits = []
            if hits:
                lines = "\n".join(_format_session_hit(hit) for hit in hits)
                sections.append(f"From past conversations:\n{lines}")

        if not sections:
            return f'No remembered facts or past conversations matched "{query}".'
        return f'Memory search for "{query}":\n\n' + "\n\n".join(sections)

    return handler


# ── ask_user (ADR-0053) ─────────────────────────────────────────────────────

#: The tool name the agent loop intercepts to suspend the turn for a clarifying question.
ASK_USER_TOOL = "ask_user"

ASK_USER_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": ASK_USER_TOOL,
        "description": (
            "Ask the user a single clarifying question and PAUSE until they answer. Call this "
            "when the request is ambiguous or missing a detail you genuinely need to proceed "
            "correctly — instead of guessing. The turn suspends; the user's reply comes back "
            "as this tool's result and you continue from there. Ask one focused question; do "
            "not use this for rhetorical questions or to confirm something you already know."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The single clarifying question to put to the user.",
                }
            },
            "required": ["question"],
        },
    },
}


def make_ask_user_handler() -> Callable[[dict[str, Any], str], Awaitable[str]]:
    """Build the ``ask_user`` safety-net handler (ADR-0053).

    The agent loop intercepts ``ask_user`` to suspend the turn (persist + emit
    ``awaiting_input`` + end the stream), so this handler is **not** used on the normal path.
    It exists only so ``ask_user`` is a registered built-in — its spec reaches the model via
    the same discovery path as ``now``/``remember`` — and so a turn that somehow reaches it
    without suspend support degrades to a clear instruction rather than failing.
    """

    async def handler(arguments: dict[str, Any], _tenant: str) -> str:
        question = str(arguments.get("question") or "").strip()
        return (
            "error: cannot pause for input right now; proceed with your best assumption "
            f"and state it. (Question was: {question})"
        )

    return handler
