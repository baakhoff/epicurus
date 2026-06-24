"""Background fact extraction — distil durable user facts from a finished exchange.

The *background path* of the memory design (ADR-0045). After a turn completes, the agent
hands the latest user message and the assistant's reply here; a single LLM call decides what,
if anything, is worth remembering about the user long-term and writes it to the
:class:`~epicurus_core_app.memory.facts.UserFactStore`. It runs off the response path (the
agent schedules it as a background task) so it never adds latency to the reply, mirroring how
ChatGPT folds details from chats into memory and how Mem0/LangMem run an extraction pass.

Everything here is best-effort: a paused gateway, a model that returns junk, or any error
yields zero facts rather than disturbing the chat.
"""

from __future__ import annotations

import json
from typing import Protocol

from epicurus_core import get_logger
from epicurus_core_app.llm.models import ChatMessage, ChatResult
from epicurus_core_app.llm.power import GatewayPausedError
from epicurus_core_app.memory.facts import SOURCE_AUTO, UserFact, UserFactStore

log = get_logger("epicurus_core_app.memory.extraction")

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
