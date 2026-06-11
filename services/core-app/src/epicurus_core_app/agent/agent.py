"""The agent orchestrator — a thin tool-calling loop (ADR-0001).

A turn: ask the LLM (offering the modules' tools via the gateway), run any tool calls
through MCP, feed the results back, and loop until the model answers or ``max_steps``
is reached. The agent talks to models only through the gateway and to modules only
through MCP — never a provider SDK. It inherits the gateway's power-state behavior.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from epicurus_core import get_logger
from epicurus_core_app.agent.mcp_host import McpHost
from epicurus_core_app.llm.gateway import LlmGateway
from epicurus_core_app.llm.models import ChatMessage

log = get_logger("epicurus_core_app.agent")


class AgentTurn(BaseModel):
    """The result of one agent turn."""

    content: str
    tools_used: list[str] = Field(default_factory=list)
    stopped: str  # "completed" or "max_steps"


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

    def __init__(self, *, gateway: LlmGateway, mcp: McpHost, max_steps: int = 4) -> None:
        self._gateway = gateway
        self._mcp = mcp
        self._max_steps = max_steps

    async def run(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tenant_id: str | None = None,
    ) -> AgentTurn:
        """Run one turn to completion (or until ``max_steps`` tool rounds)."""
        specs, route = await self._mcp.discover()
        convo = list(messages)
        tools_used: list[str] = []
        for _ in range(self._max_steps):
            result = await self._gateway.chat(
                convo, model=model, tools=specs or None, tenant_id=tenant_id
            )
            if not result.tool_calls:
                return AgentTurn(content=result.content, tools_used=tools_used, stopped="completed")
            convo.append(
                ChatMessage(role="assistant", content=result.content, tool_calls=result.tool_calls)
            )
            for call in result.tool_calls:
                name, arguments, call_id = _parse_tool_call(call)
                tools_used.append(name)
                output = await self._invoke(name, arguments, route)
                convo.append(
                    ChatMessage(role="tool", tool_call_id=call_id, name=name, content=output)
                )
        final = await self._gateway.chat(convo, model=model, tenant_id=tenant_id)
        return AgentTurn(content=final.content, tools_used=tools_used, stopped="max_steps")

    async def _invoke(self, name: str, arguments: dict[str, Any], route: dict[str, str]) -> str:
        url = route.get(name)
        if url is None:
            return f"error: unknown tool {name!r}"
        try:
            return await self._mcp.call(name, arguments, url)
        except Exception as exc:  # surface the failure to the model, don't crash the turn
            log.warning("tool call failed", tool=name, error=str(exc))
            return f"error: tool {name!r} failed: {exc}"
