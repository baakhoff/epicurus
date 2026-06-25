"""Background fact extraction — distil durable user facts from a finished exchange.

The *background path* of the memory design (ADR-0045). A finished exchange (the latest user
message + the assistant's reply) becomes a single LLM call that decides what, if anything, is
worth remembering about the user long-term and writes it to the
:class:`~epicurus_core_app.memory.facts.UserFactStore` — mirroring how ChatGPT folds details
from chats into memory and how Mem0/LangMem run an extraction pass.

:class:`FactExtractor` is that unit of work. *When* it runs is the operator's choice (ADR-0051):
either immediately after the turn (the agent fires it as a background task) or — the default —
*deferred* to a nightly window, where :class:`ExtractionRunner` drains the durable
:class:`~epicurus_core_app.memory.extraction_queue.ExtractionQueue` serially so extraction never
competes with a live turn for the one local GPU.

Everything here is best-effort: a paused gateway, a model that returns junk, or any error
yields zero facts rather than disturbing the chat.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Protocol
from zoneinfo import ZoneInfo

from epicurus_core import get_logger
from epicurus_core_app.llm.models import ChatMessage, ChatResult
from epicurus_core_app.llm.power import GatewayPausedError
from epicurus_core_app.memory.extraction_queue import ExtractionQueue
from epicurus_core_app.memory.facts import SOURCE_AUTO, UserFact, UserFactStore

log = get_logger("epicurus_core_app.memory.extraction")

# IANA timezone provider — the runner schedules its nightly window in the operator's tz (#271).
TimezoneProvider = Callable[[], Awaitable[str]]

# Bound what we feed the extractor and what we accept back, so one turn can't blow up the
# prompt or flood memory.
_INPUT_CAP = 4000
_MAX_FACTS = 6
_FACT_CAP = 280

_SYSTEM_PROMPT = (
    "You are the memory step of a personal assistant. From the latest exchange between the "
    "user and the assistant, extract durable facts worth remembering about the USER for "
    "future conversations.\n\n"
    "Save a fact only if it is:\n"
    "- about the user themselves — their identity, stable preferences, ongoing situation, "
    "projects, relationships, or how they want the assistant to behave;\n"
    "- durable — likely still true weeks from now, not a one-off task detail or passing mood.\n\n"
    "Do NOT save:\n"
    "- transient task details, questions, or anything specific to only this conversation;\n"
    "- general world knowledge, or facts about other people or things not tied to the user;\n"
    "- anything about the assistant itself;\n"
    "- secrets, passwords, API keys, or other sensitive credentials.\n\n"
    "Write each fact as ONE short, standalone third-person statement, e.g. "
    '"Prefers concise answers.", "Is building a local-first assistant called epicurus.", '
    '"Lives in Belgrade."\n\n'
    "Respond with ONLY a JSON array of strings — the new facts — or [] if there is nothing "
    "worth saving. No prose, no code fences."
)


class _ChatModel(Protocol):
    """The slice of the LLM gateway the extractor needs (eases faking in tests)."""

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = ...,
        tools: list[dict[str, object]] | None = ...,
        tenant_id: str | None = ...,
    ) -> ChatResult: ...


class FactExtractor:
    """Turns a completed exchange into zero or more saved :class:`UserFact` rows."""

    def __init__(
        self,
        chat: _ChatModel,
        facts: UserFactStore,
        *,
        model: str | None = None,
    ) -> None:
        self._chat = chat
        self._facts = facts
        # Optional dedicated extraction model; ``None`` uses the operator's default model.
        self._model = model

    async def extract(self, *, tenant: str, user_text: str, assistant_text: str) -> list[UserFact]:
        """Extract and persist new user facts from one exchange (best-effort).

        Returns the facts actually saved (deduped against what's already remembered). Any
        failure — a paused gateway, a malformed reply, a storage error — logs and returns
        ``[]``; memory extraction must never break or delay a chat.
        """
        user_text = (user_text or "").strip()
        if not user_text:
            return []
        prompt = (
            f"User said:\n{user_text[:_INPUT_CAP]}\n\n"
            f"Assistant replied:\n{(assistant_text or '').strip()[:_INPUT_CAP]}"
        )
        messages = [
            ChatMessage(role="system", content=_SYSTEM_PROMPT),
            ChatMessage(role="user", content=prompt),
        ]
        try:
            result = await self._chat.chat(messages, model=self._model, tenant_id=tenant)
        except GatewayPausedError:
            return []  # paused — nothing to do, not an error
        except Exception as exc:  # extraction is an enhancement, never a hard dependency
            log.warning("fact extraction call failed", error=str(exc))
            return []

        candidates = _parse_facts(result.content)
        saved: list[UserFact] = []
        for text in candidates:
            try:
                fact = await self._facts.save(tenant=tenant, text=text, source=SOURCE_AUTO)
            except Exception as exc:  # one bad write must not drop the rest
                log.warning("fact save failed", error=str(exc))
                continue
            if fact is not None:
                saved.append(fact)
        if saved:
            log.info("remembered facts about the user", count=len(saved), tenant=tenant)
        return saved


def _parse_facts(content: str) -> list[str]:
    """Pull a clean list of fact strings out of the model's reply (tolerant of stray text).

    Accepts a bare JSON array, or one wrapped in code fences / surrounding prose, by taking
    the outermost ``[...]`` span. Anything unparseable yields ``[]``.
    """
    if not content:
        return []
    start, end = content.find("["), content.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        parsed = json.loads(content[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    facts: list[str] = []
    for item in parsed:
        if isinstance(item, str) and item.strip():
            facts.append(item.strip()[:_FACT_CAP])
        if len(facts) >= _MAX_FACTS:
            break
    return facts


class _Power(Protocol):
    """The slice of the power controller the runner needs (eases faking in tests)."""

    @property
    def paused(self) -> bool: ...


def _seconds_until_next_run(now: datetime, hour: int) -> float:
    """Seconds from ``now`` until the next ``hour``:00 in ``now``'s own timezone.

    ``now`` must be timezone-aware. When it is already at or past ``hour``:00 today, the next
    run is ``hour``:00 tomorrow, so the result is always strictly positive — the loop can never
    busy-spin on a zero-length sleep.
    """
    target = now.replace(hour=hour % 24, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


class ExtractionRunner:
    """Drains the extraction queue on a nightly schedule — serially, and only when available.

    The deferred half of ADR-0051. The agent enqueues finished exchanges all day; this runner
    waits for a quiet hour (the operator's local night) and distils them then, one at a time, so
    fact extraction never competes with a live turn for the GPU. Fire-and-forget from the app
    lifespan; cancelled on shutdown.
    """

    def __init__(
        self,
        queue: ExtractionQueue,
        extractor: FactExtractor,
        power: _Power,
        *,
        timezone: TimezoneProvider,
        hour: int = 3,
        batch_limit: int = 200,
    ) -> None:
        self._queue = queue
        self._extractor = extractor
        self._power = power
        self._timezone = timezone
        self._hour = hour % 24
        self._batch_limit = batch_limit

    async def drain_once(self, *, batch_limit: int | None = None) -> int:
        """Extract facts from every pending exchange, oldest first; returns how many ran.

        Serial by design — one extraction at a time keeps the batch gentle on a single local
        GPU. Skips entirely while the gateway is paused (the model is asleep), and stops
        mid-batch if it is paused under us, leaving the rest queued for the next window. A
        processed exchange is removed whether or not it yielded a fact: the extractor is
        best-effort, so a row that distils to nothing — or one bad row — must not wedge the
        queue forever.
        """
        if self._power.paused:
            log.info("nightly extraction skipped; gateway paused")
            return 0
        limit = batch_limit if batch_limit is not None else self._batch_limit
        items = await self._queue.pending(limit=limit)
        processed = 0
        for item in items:
            if self._power.paused:  # paused under us — leave the remainder for the next window
                log.info("nightly extraction paused mid-drain", remaining=len(items) - processed)
                break
            try:
                await self._extractor.extract(
                    tenant=item.tenant,
                    user_text=item.user_text,
                    assistant_text=item.assistant_text,
                )
            except Exception as exc:  # the extractor swallows its own errors; this is a backstop
                log.warning("queued extraction failed", task_id=item.id, error=str(exc))
            await self._queue.delete([item.id])
            processed += 1
        if processed:
            log.info("nightly extraction drained the queue", count=processed)
        return processed

    async def _sleep_until_next_run(self) -> None:
        """Sleep until the next nightly window, in the operator's timezone (UTC if unknown)."""
        tz: tzinfo
        try:
            tz = ZoneInfo((await self._timezone()).strip() or "UTC")
        except Exception:  # unknown / blank / bad tz — fall back to UTC rather than skip a run
            tz = UTC
        await asyncio.sleep(_seconds_until_next_run(datetime.now(tz), self._hour))

    async def run_periodic(self) -> None:
        """Loop forever: wait for the nightly window, drain the queue, repeat.

        Each iteration is self-contained — a failed drain logs and waits for the next window
        rather than killing the loop.
        """
        while True:
            await self._sleep_until_next_run()
            try:
                await self.drain_once()
            except Exception as exc:  # never let the scheduler die on a transient error
                log.warning("nightly extraction run failed", error=str(exc))
