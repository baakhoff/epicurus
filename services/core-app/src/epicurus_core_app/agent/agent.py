"""The agent orchestrator — a thin tool-calling loop (ADR-0001).

A turn: ask the LLM (offering the modules' tools via the gateway), run any tool calls
through MCP, feed the results back, and loop until the model answers or ``max_steps``
is reached. The agent talks to models only through the gateway and to modules only
through MCP — never a provider SDK. It inherits the gateway's power-state behavior.

``run`` resolves a turn in one response; ``run_stream`` yields the same turn as it
happens — content deltas, tool progress, then the final turn — for the web UI.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Coroutine
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from epicurus_core import Attachment, EntityRef, ToolEnvelope, get_logger
from epicurus_core_app.agent.activity import (
    ActivityItem,
    MessageActivity,
    activity_from_timeline,
    append_thinking,
    append_tool,
)
from epicurus_core_app.agent.builtins import ASK_USER_TOOL
from epicurus_core_app.agent.mcp_host import McpHost, ToolCallError
from epicurus_core_app.agent.suspended import SuspendedRunStore
from epicurus_core_app.llm.gateway import LlmGateway
from epicurus_core_app.llm.models import ChatMessage, ChatResult
from epicurus_core_app.llm.prefs import LlmPrefsStore
from epicurus_core_app.memory.extraction import FactExtractor
from epicurus_core_app.memory.extraction_queue import ExtractionQueue
from epicurus_core_app.memory.memory import Memory
from epicurus_core_app.readiness import Readiness

log = get_logger("epicurus_core_app.agent")

# Persisted-activity bounds: a reasoning trace can be very long, so cap what is stored, and
# keep a tool step's argument detail short (it's a glanceable hint, not the full payload).
_THINKING_CAP = 20_000
_TOOL_DETAIL_CAP = 500

# How long recall (a single embedding round-trip) may take before the turn proceeds without it.
# Recall is the one memory step still on the response path; a cold or busy embedder must never
# delay the first token (ADR-0051). The best-effort assemble already degrades to no recall.
# 4s (was 2s): on a single GPU the recall embed often forces an Ollama model swap (chat model
# out, embed model in) that 2s could not outlast, so recall was skipped on nearly every turn.
# Operators who keep the embed model warm can lower this; slower hardware can raise it.
_DEFAULT_RECALL_TIMEOUT_S = 4.0

# A reasoning model (qwen3, deepseek-r1, …) sometimes emits its <think> block and then stops —
# no answer text and no tool call. The loop would end the turn with empty content, which renders
# as a silent "stop" (an activity trace with no answer bubble). Nudge it once to commit to an
# answer, then continue the loop (bounded by max_steps); if it still says nothing, fall back to a
# clear message rather than persisting an empty turn.
_ANSWER_NUDGE = "Please continue and give your final answer to my last message."
_EMPTY_ANSWER_FALLBACK = (
    "I wasn't able to produce an answer that time — please try again or rephrase your request."
)


def _tool_detail(arguments: dict[str, Any]) -> str | None:
    """Compact JSON of a tool call's arguments for the step's expandable detail (or None)."""
    if not arguments:
        return None
    try:
        rendered = json.dumps(arguments, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return rendered[:_TOOL_DETAIL_CAP]


class AgentTurn(BaseModel):
    """The result of one agent turn."""

    content: str
    tools_used: list[str] = Field(default_factory=list)
    stopped: str  # "completed" or "max_steps"
    # Module entities the turn referenced, lifted from tool outputs (ADR-0019).
    entity_refs: list[EntityRef] = Field(default_factory=list)
    # The turn's process — thinking + tool steps — persisted so the activity timeline
    # survives a reopen, not only the live stream (ADR-0041).
    activity: MessageActivity = Field(default_factory=MessageActivity)


def _extract_entities(output: str) -> tuple[str, list[EntityRef]]:
    """Split a tool's output into (text for the model, entity references).

    A tool may return a JSON :class:`ToolEnvelope` (``{text, entity_refs}``); if so the
    text is fed back to the model and the refs are lifted onto the turn. Anything else —
    plain text, an ``error:`` string, or unrelated JSON — is returned unchanged with no
    refs, so existing tools keep working.
    """
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        return output, []
    if not (
        isinstance(data, dict)
        and isinstance(data.get("text"), str)
        and isinstance(data.get("entity_refs"), list)
    ):
        return output, []
    try:
        envelope = ToolEnvelope.model_validate(data)
    except ValidationError:
        return output, []
    return envelope.text, envelope.entity_refs


class _RefCollector:
    """Accumulates entity references across a turn's tool calls, de-duplicated."""

    def __init__(self) -> None:
        self.refs: list[EntityRef] = []
        self._seen: set[tuple[str, str, str]] = set()

    def add(self, refs: list[EntityRef]) -> None:
        for ref in refs:
            key = (ref.module, ref.kind, ref.ref_id)
            if key not in self._seen:
                self._seen.add(key)
                self.refs.append(ref)


class AttachmentExpander(Protocol):
    """Resolves a turn's attachments into a text block the agent injects (ADR-0019)."""

    async def expand(self, attachments: list[Attachment], *, tenant: str) -> str: ...


class AgentEvent(BaseModel):
    """One event of a streaming agent turn (the SSE protocol's payload).

    ``delta`` carries a content token; ``tool`` reports a tool call's progress
    (``running`` → ``ok``/``error``); ``done`` carries the final turn; ``error``
    ends a failed stream. A ``readiness`` event may *lead* the stream (warming
    progress; emitted by the route, not the loop) — see ADR-0027. ``awaiting_input``
    ends the stream when the model calls ``ask_user``: it carries the ``question`` and a
    ``run_id`` the client posts the answer to, to resume the turn (ADR-0053).
    """

    type: str  # "delta" | "tool" | "done" | "error" | "readiness" | "awaiting_input"
    text: str | None = None
    tool: str | None = None
    status: str | None = None
    turn: AgentTurn | None = None
    detail: str | None = None
    readiness: Readiness | None = None
    # awaiting_input (ask_user, ADR-0053): the question to put to the user + the run to resume.
    run_id: str | None = None
    question: str | None = None


def _parse_tool_call(call: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    """Pull ``(name, arguments, id)`` out of an OpenAI-style tool call."""
    function = call.get("function") or {}
    name = function.get("name") or ""
    raw = function.get("arguments")
    if isinstance(raw, dict):
        arguments = raw
    elif isinstance(raw, str):
        try:
            arguments = json.loads(raw or "{}")
        except json.JSONDecodeError:
            arguments = {}
    else:
        arguments = {}
    return name, arguments, call.get("id") or ""


class Agent:
    """Drives the LLM gateway plus module tools to answer a turn."""

    def __init__(
        self,
        *,
        gateway: LlmGateway,
        mcp: McpHost,
        memory: Memory | None = None,
        max_steps: int = 4,
        default_tenant: str = "local",
        attachments: AttachmentExpander | None = None,
        extractor: FactExtractor | None = None,
        queue: ExtractionQueue | None = None,
        defer_extraction: bool = True,
        recall_timeout_s: float = _DEFAULT_RECALL_TIMEOUT_S,
        prefs: LlmPrefsStore | None = None,
        suspended: SuspendedRunStore | None = None,
    ) -> None:
        self._gateway = gateway
        self._mcp = mcp
        self._memory = memory
        self._max_steps = max_steps
        self._default_tenant = default_tenant
        self._attachments = attachments
        # Fact extraction (ADR-0045/0051): after a turn, distil durable user facts. By default
        # the exchange is *deferred* to ``queue`` and a nightly runner distils it off-hours, so
        # extraction never competes with the next turn for the GPU. ``defer_extraction=False``
        # restores the immediate path — fire ``extractor`` as a background task. Tasks are
        # tracked so they aren't GC'd mid-flight.
        self._extractor = extractor
        self._queue = queue
        self._defer_extraction = defer_extraction
        self._recall_timeout_s = recall_timeout_s
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._prefs = prefs
        # Suspend store for ask_user pause/resume (ADR-0053). None disables pausing (the loop
        # then degrades: ask_user gets an instruction to proceed rather than suspending).
        self._suspended = suspended

    async def _effective_max_steps(self, tenant_id: str | None) -> int:
        """The active agent loop bound: the stored pref if set, else the env default.

        Resolved per turn so the operator's UI choice takes effect without a restart
        (the agent is constructed once). The route clamps the stored value's range.
        """
        if self._prefs is not None:
            stored = await self._prefs.get_agent_max_steps(tenant_id or self._default_tenant)
            if stored is not None:
                return stored
        return self._max_steps

    async def run(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
    ) -> AgentTurn:
        """Run one turn to completion (or until ``max_steps`` tool rounds).

        With ``session_id`` and memory configured, the turn is grounded in the
        session's prior messages plus semantically recalled context, and both the
        new user input and the answer are persisted for future turns.
        """
        tenant = tenant_id or self._default_tenant
        messages = await self._expand_attachments(messages, tenant=tenant)
        convo = await self._assemble(messages, tenant=tenant, session_id=session_id)
        turn = await self._loop(convo, model=model, tenant_id=tenant_id)
        await self._persist_answer(turn, tenant=tenant, session_id=session_id)
        self._schedule_extraction(tenant=tenant, messages=messages, answer=turn.content)
        return turn

    async def run_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
        persist_input: bool = True,
        resume_convo: list[ChatMessage] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Stream one turn as it happens: deltas, tool progress, then ``done``.

        Memory semantics match :meth:`run`. The turn's content is exactly the text
        the caller watched stream by. A failure mid-stream ends with an ``error``
        event rather than an exception (the HTTP response has already started).

        ``persist_input=False`` with empty ``messages`` re-answers the stored tail without
        adding a new user message (regenerate / edit, #302).

        ``resume_convo`` continues a turn that ``ask_user`` paused (ADR-0053): the caller
        passes the rehydrated conversation (history + the answer as the pending tool result),
        so assembly/persistence of new input is skipped and the loop just continues.
        """
        tenant = tenant_id or self._default_tenant
        max_steps = await self._effective_max_steps(tenant)
        if resume_convo is not None:
            convo = resume_convo
        else:
            messages = await self._expand_attachments(messages, tenant=tenant)
            convo = await self._assemble(
                messages, tenant=tenant, session_id=session_id, persist_input=persist_input
            )
        parts: list[str] = []
        timeline: list[ActivityItem] = []
        tools_used: list[str] = []
        refs = _RefCollector()
        stopped = "completed"
        reasoned = False  # the model emitted <think> reasoning at least once this turn
        nudged = False  # we already nudged a blank step to commit to an answer (do it once)
        try:
            specs, route = await self._mcp.discover()
            # Offer tools only to a model that can use them; otherwise the runtime errors and
            # the turn fails. A tool-less model just answers in text (the UI flags it).
            can_use_tools = bool(specs) and await self._gateway.supports_tools(model, tenant_id)
            offer = specs if can_use_tools else None
            for _ in range(max_steps):
                result: ChatResult | None = None
                answer_before = len(parts)
                async for event in self._gateway.stream_chat(
                    convo, model=model, tools=offer, tenant_id=tenant_id
                ):
                    if event.delta:
                        parts.append(event.delta)
                        yield AgentEvent(type="delta", text=event.delta)
                    if event.reasoning:
                        reasoned = True
                        append_thinking(timeline, event.reasoning)
                        yield AgentEvent(type="thinking", text=event.reasoning)
                    if event.result is not None:
                        result = event.result
                if result is None or not result.tool_calls:
                    # The model answered (text streamed) or it produced nothing. If nothing — a
                    # reasoning model that thought but never answered — nudge it once and retry,
                    # rather than ending the turn empty (which renders as the silent "stop").
                    if len(parts) > answer_before or nudged:
                        break
                    nudged = True
                    convo.append(
                        ChatMessage(role="assistant", content=result.content if result else "")
                    )
                    convo.append(ChatMessage(role="user", content=_ANSWER_NUDGE))
                    continue
                convo.append(
                    ChatMessage(
                        role="assistant", content=result.content, tool_calls=result.tool_calls
                    )
                )
                pending: tuple[str, str] | None = None  # (call_id, question) for ask_user
                for call in result.tool_calls:
                    name, arguments, call_id = _parse_tool_call(call)
                    if name == ASK_USER_TOOL:
                        # Don't execute — ask_user suspends the turn (ADR-0053). Defer the
                        # suspend until this step's other calls have run, so every tool_call
                        # gets a result and the conversation stays valid on resume.
                        if pending is None:
                            pending = (call_id, str(arguments.get("question") or "").strip())
                            tools_used.append(name)
                            append_tool(timeline, name, "ok", _tool_detail(arguments))
                            yield AgentEvent(
                                type="tool", tool=name, status="ok", detail=pending[1] or None
                            )
                        else:  # a second ask_user in one step — stub it so the convo stays valid
                            convo.append(
                                ChatMessage(
                                    role="tool",
                                    tool_call_id=call_id,
                                    name=name,
                                    content="(answered together with the question above)",
                                )
                            )
                        continue
                    tools_used.append(name)
                    detail = _tool_detail(arguments)
                    yield AgentEvent(type="tool", tool=name, status="running", detail=detail)
                    output = await self._invoke(name, arguments, route, tenant=tenant)
                    text, found = _extract_entities(output)
                    refs.add(found)
                    status = "error" if text.startswith("error:") else "ok"
                    yield AgentEvent(type="tool", tool=name, status=status, detail=detail)
                    append_tool(timeline, name, status, detail)
                    convo.append(
                        ChatMessage(role="tool", tool_call_id=call_id, name=name, content=text)
                    )
                if pending is not None:
                    call_id, question = pending
                    run_id = await self._suspend(
                        convo=convo,
                        model=model,
                        tenant=tenant,
                        session_id=session_id,
                        pending_call_id=call_id,
                        question=question,
                    )
                    if run_id is not None:
                        # Pause the turn: the client posts the answer to resume (no `done`).
                        yield AgentEvent(type="awaiting_input", run_id=run_id, question=question)
                        return
                    # No suspend store wired — degrade: give the model a result and keep going.
                    convo.append(
                        ChatMessage(
                            role="tool",
                            tool_call_id=call_id,
                            name=ASK_USER_TOOL,
                            content="error: cannot pause for input; use your best assumption",
                        )
                    )
            else:  # steps exhausted — stream one final answer without tools
                stopped = "max_steps"
                async for event in self._gateway.stream_chat(
                    convo, model=model, tenant_id=tenant_id
                ):
                    if event.delta:
                        parts.append(event.delta)
                        yield AgentEvent(type="delta", text=event.delta)
                    if event.reasoning:
                        reasoned = True
                        append_thinking(timeline, event.reasoning)
                        yield AgentEvent(type="thinking", text=event.reasoning)
        except Exception as exc:  # the response already started — finish with an error event
            log.warning("streaming turn failed", error=str(exc))
            yield AgentEvent(type="error", detail=str(exc))
            return
        content = "".join(parts)
        if not content.strip():
            # No tool calls and no answer text, even after a nudge — never persist or stream an
            # empty turn (it renders as a silent stop). Surface a clear fallback, and log why so
            # the operator can see it was e.g. a reasoning model that thought but never answered.
            log.warning(
                "turn produced no answer; using fallback",
                model=model,
                stopped=stopped,
                reasoned=reasoned,
                nudged=nudged,
            )
            content = _EMPTY_ANSWER_FALLBACK
            yield AgentEvent(type="delta", text=content)
        turn = AgentTurn(
            content=content,
            tools_used=tools_used,
            stopped=stopped,
            entity_refs=refs.refs,
            activity=activity_from_timeline(timeline, thinking_cap=_THINKING_CAP),
        )
        # Shield only the answer write: the model already produced the reply, so a cancellation
        # arriving now (server shutdown — the turn runs in a detached task, see live_runs.py)
        # must still flush it. The model call above stays promptly cancellable; losing the
        # finished answer here would be the very bug this decoupling fixes (#376).
        await asyncio.shield(self._persist_answer(turn, tenant=tenant, session_id=session_id))
        self._schedule_extraction(tenant=tenant, messages=messages, answer=turn.content)
        yield AgentEvent(type="done", turn=turn)

    def _schedule_extraction(
        self, *, tenant: str, messages: list[ChatMessage], answer: str
    ) -> None:
        """Persist this exchange for fact extraction, off the response path (ADR-0045/0051).

        Default (deferred): enqueue the exchange for the nightly runner — a quick, durable
        insert, so extraction won't compete with the next turn for the GPU. Immediate mode
        (``defer_extraction=False``): fire the extractor now as a background task (the legacy
        ADR-0045 path). Either way it is fire-and-forget so it never delays the reply, and the
        task is tracked until it finishes so it isn't garbage-collected mid-flight. Skips when
        there is nothing to learn from — no user text, no answer, or only the empty-answer
        fallback (a canned non-answer) — or when no sink is configured.
        """
        if not answer or answer == _EMPTY_ANSWER_FALLBACK:
            return
        user_text = next(
            (m.content for m in reversed(messages) if m.role == "user" and m.content), None
        )
        if not user_text:
            return
        coro: Coroutine[Any, Any, object]
        if self._defer_extraction and self._queue is not None:
            coro = self._queue.enqueue(tenant=tenant, user_text=user_text, assistant_text=answer)
        elif self._extractor is not None:
            coro = self._extractor.extract(
                tenant=tenant, user_text=user_text, assistant_text=answer
            )
        else:
            return
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _persist_answer(
        self, turn: AgentTurn, *, tenant: str, session_id: str | None
    ) -> None:
        if self._memory is None or not session_id:
            return
        try:
            await self._memory.remember(
                tenant=tenant,
                session_id=session_id,
                role="assistant",
                content=turn.content,
                entity_refs=[ref.model_dump() for ref in turn.entity_refs],
                # Persist the process only when there is one — keep plain turns blob-free.
                activity=None if turn.activity.is_empty() else turn.activity.model_dump(),
            )
        except Exception as exc:  # a failed write must not lose the answer
            log.warning("memory write failed", error=str(exc))

    async def _suspend(
        self,
        *,
        convo: list[ChatMessage],
        model: str | None,
        tenant: str,
        session_id: str | None,
        pending_call_id: str,
        question: str,
    ) -> str | None:
        """Persist the in-progress run so an answer can resume it (ADR-0053).

        Returns the run id, or ``None`` when no suspend store is wired — the caller then
        degrades gracefully (instructs the model to proceed) instead of pausing.
        """
        if self._suspended is None:
            return None
        try:
            return await self._suspended.save(
                tenant=tenant,
                session_id=session_id,
                model=model,
                pending_call_id=pending_call_id,
                question=question,
                conversation=[m.model_dump(exclude_none=True) for m in convo],
            )
        except Exception as exc:  # a failed persist must not crash the turn
            log.warning("suspend persist failed; proceeding without pause", error=str(exc))
            return None

    async def _expand_attachments(
        self, messages: list[ChatMessage], *, tenant: str
    ) -> list[ChatMessage]:
        """Resolve any attachments on the user's message into a leading system message.

        Best-effort (ADR-0019): an expander failure or empty result leaves the turn
        untouched. The attachments themselves stay on the user message (persisted +
        stripped before any provider call); only their resolved content is injected here.
        """
        if self._attachments is None:
            return messages
        attached = [a for m in messages if m.role == "user" for a in (m.attachments or [])]
        if not attached:
            return messages
        try:
            context = await self._attachments.expand(attached, tenant=tenant)
        except Exception as exc:  # attachments are an enhancement, never a hard dependency
            log.warning("attachment expansion failed; proceeding without it", error=str(exc))
            return messages
        if not context:
            return messages
        preamble = ChatMessage(role="system", content=f"Attached context:\n{context}")
        return [preamble, *messages]

    async def _recall_within_budget(self, *, tenant: str, query: str) -> list[str]:
        """Recall the facts relevant to ``query``, bounded by ``recall_timeout_s`` (ADR-0051).

        Recall embeds the query — the one memory step still on the response path. A cold or busy
        embedder must not delay the first token, so it is time-boxed; on timeout or any error the
        turn proceeds with no recalled facts (the same best-effort degrade as the rest of
        assemble), rather than blocking until our interaction with the model itself stalls.
        """
        if self._memory is None:
            return []
        start = time.monotonic()
        try:
            return await asyncio.wait_for(
                self._memory.recall(tenant=tenant, query=query), self._recall_timeout_s
            )
        except TimeoutError:
            # The embed didn't finish in the budget — usually a cold or busy embedder (on a
            # single GPU, an Ollama model swap). Degrade to no recall; name the budget so the
            # operator can see it timed out (vs. failed) and tune MEMORY_RECALL_TIMEOUT_S.
            log.warning(
                "recall skipped: embed timed out",
                timeout_s=self._recall_timeout_s,
                elapsed_s=round(time.monotonic() - start, 2),
            )
            return []
        except Exception as exc:  # embedder/Qdrant trouble — degrade to no recall, never block
            log.warning(
                "recall skipped: backend error",
                error=str(exc),
                error_type=type(exc).__name__,
                elapsed_s=round(time.monotonic() - start, 2),
            )
            return []

    async def _assemble(
        self,
        messages: list[ChatMessage],
        *,
        tenant: str,
        session_id: str | None,
        persist_input: bool = True,
    ) -> list[ChatMessage]:
        """Prepend recalled context + session history, then persist the new input.

        Memory is best-effort: any failure (DB, Qdrant, embeddings) degrades to a
        plain turn rather than breaking the chat.

        With ``persist_input=False`` and no new ``messages`` this re-answers the stored tail
        (regenerate / edit, #302): the recall query falls back to the last *stored* user turn,
        and nothing new is persisted — the user message is already in history.
        """
        if self._memory is None or not session_id:
            return list(messages)
        try:
            convo: list[ChatMessage] = []
            history = await self._memory.history(tenant=tenant, session_id=session_id)
            # Recall off the new user input, else (re-answer) the last user turn in history.
            last_user = next(
                (m.content for m in reversed(messages) if m.role == "user" and m.content), None
            ) or next(
                (m.content for m in reversed(history) if m.role == "user" and m.content), None
            )
            if last_user:
                recalled = await self._recall_within_budget(tenant=tenant, query=last_user)
                if recalled:
                    joined = "\n".join(f"- {fact}" for fact in recalled)
                    convo.append(
                        ChatMessage(
                            role="system",
                            content=(
                                "What you remember about the user (use it when relevant; "
                                f"don't recite it):\n{joined}"
                            ),
                        )
                    )
            convo.extend(history)
            convo.extend(messages)
            if persist_input:
                for message in messages:
                    if message.role == "user" and message.content:
                        await self._memory.remember(
                            tenant=tenant,
                            session_id=session_id,
                            role="user",
                            content=message.content,
                            attachments=(
                                [a.model_dump() for a in message.attachments]
                                if message.attachments
                                else None
                            ),
                        )
            return convo
        except Exception as exc:  # memory is an enhancement, never a hard dependency
            log.warning("memory read failed; proceeding without it", error=str(exc))
            return list(messages)

    async def _loop(
        self, messages: list[ChatMessage], *, model: str | None, tenant_id: str | None
    ) -> AgentTurn:
        """The tool-calling loop: ask, run tools, feed results back, until an answer."""
        specs, route = await self._mcp.discover()
        call_tenant = tenant_id or self._default_tenant
        max_steps = await self._effective_max_steps(tenant_id)
        # Offer tools only to a tool-capable model (else the runtime errors); a tool-less model
        # just answers in text.
        offer = specs if specs and await self._gateway.supports_tools(model, tenant_id) else None
        convo = list(messages)
        tools_used: list[str] = []
        timeline: list[ActivityItem] = []
        refs = _RefCollector()

        def activity() -> MessageActivity:
            return activity_from_timeline(timeline, thinking_cap=_THINKING_CAP)

        reasoned = False  # the model emitted <think> reasoning at least once this turn
        nudged = False  # we already nudged a blank step to commit to an answer (do it once)
        content = ""
        stopped = "completed"
        for _ in range(max_steps):
            result = await self._gateway.chat(convo, model=model, tools=offer, tenant_id=tenant_id)
            if result.reasoning:
                reasoned = True
                append_thinking(timeline, result.reasoning)
            if not result.tool_calls:
                # The model answered, or (a reasoning model) thought but produced nothing. If it
                # said nothing, nudge it once to commit to an answer rather than ending empty —
                # the same silent "stop" the streamed path guards against.
                if result.content.strip() or nudged:
                    content = result.content
                    break
                nudged = True
                convo.append(ChatMessage(role="assistant", content=result.content))
                convo.append(ChatMessage(role="user", content=_ANSWER_NUDGE))
                continue
            convo.append(
                ChatMessage(role="assistant", content=result.content, tool_calls=result.tool_calls)
            )
            for call in result.tool_calls:
                name, arguments, call_id = _parse_tool_call(call)
                tools_used.append(name)
                output = await self._invoke(name, arguments, route, tenant=call_tenant)
                text, found = _extract_entities(output)
                refs.add(found)
                status = "error" if text.startswith("error:") else "ok"
                append_tool(timeline, name, status, _tool_detail(arguments))
                convo.append(
                    ChatMessage(role="tool", tool_call_id=call_id, name=name, content=text)
                )
        else:
            final = await self._gateway.chat(convo, model=model, tenant_id=tenant_id)
            if final.reasoning:
                reasoned = True
                append_thinking(timeline, final.reasoning)
            content = final.content
            stopped = "max_steps"
        if not content.strip():
            log.warning(
                "turn produced no answer; using fallback",
                model=model,
                stopped=stopped,
                reasoned=reasoned,
                nudged=nudged,
            )
            content = _EMPTY_ANSWER_FALLBACK
        return AgentTurn(
            content=content,
            tools_used=tools_used,
            stopped=stopped,
            entity_refs=refs.refs,
            activity=activity(),
        )

    async def _invoke(
        self, name: str, arguments: dict[str, Any], route: dict[str, str], *, tenant: str
    ) -> str:
        url = route.get(name)
        if url is None:
            return f"error: unknown tool {name!r}"
        try:
            return await self._mcp.call(name, arguments, url, tenant=tenant)
        except ToolCallError as exc:
            # The tool ran and reported failure; hand the model the tool's own error
            # text — exactly what it received before the host raised on isError (#435).
            log.warning("tool reported failure", tool=name, error=str(exc))
            return str(exc)
        except Exception as exc:  # surface the failure to the model, don't crash the turn
            log.warning("tool call failed", tool=name, error=str(exc))
            return f"error: tool {name!r} failed: {exc}"
