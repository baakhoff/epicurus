"""Tests for the shared chat contract (ADR-0021) — one source of truth."""

from __future__ import annotations

import epicurus_core
from epicurus_core import ChatMessage, ChatResult, PlatformChatResponse, PlatformMessage


def test_chat_types_are_exported() -> None:
    assert {"ChatMessage", "ChatResult", "Role"} <= set(epicurus_core.__all__)


def test_platform_aliases_are_the_canonical_types() -> None:
    # The historical names are aliases of the canonical contract, not copies —
    # so there is exactly one definition of each shape.
    assert PlatformMessage is ChatMessage
    assert PlatformChatResponse is ChatResult


def test_platform_client_reexports_the_aliases() -> None:
    # ``from epicurus_core.platform_client import PlatformChatResponse`` must keep
    # resolving for existing module code.
    from epicurus_core.platform_client import (
        PlatformChatResponse as ClientResponse,
    )
    from epicurus_core.platform_client import (
        PlatformMessage as ClientMessage,
    )

    assert ClientResponse is ChatResult
    assert ClientMessage is ChatMessage


def test_chat_message_round_trips() -> None:
    msg = ChatMessage(role="user", content="hi")
    assert msg.model_dump(exclude_none=True) == {"role": "user", "content": "hi"}


def test_chat_result_round_trips() -> None:
    result = ChatResult(model="ollama_chat/llama3.2", content="hello", completion_tokens=2)
    dumped = result.model_dump()
    assert dumped["model"] == "ollama_chat/llama3.2"
    assert dumped["content"] == "hello"
    assert dumped["completion_tokens"] == 2
