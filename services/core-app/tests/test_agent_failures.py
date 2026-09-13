"""What a failed streaming turn tells the user — and what it never tells them (ADR-0142).

The redaction cases run against the **real** payloads from #947 and #944, kept verbatim in
``tests/fixtures/`` rather than paraphrased: the whole point of the helper is that it survives
three layers of an intermediary's escaping, and a hand-simplified blob would not exercise that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from epicurus_core_app.agent.failures import (
    STREAM_STALLED_MESSAGE,
    classify_stream_failure,
    is_plain_sentence,
    readable_provider_message,
)
from epicurus_core_app.llm.errors import ModelCapabilityError
from epicurus_core_app.llm.power import GatewayPausedError

_FIXTURES = Path(__file__).parent / "fixtures"

# #947's transcript, verbatim (only the account identifier is a stand-in). OpenRouter wrapping a
# provider wrapping vLLM, three escape levels deep, with the real refusal at the bottom.
NEXTBIT_PAYLOAD = (_FIXTURES / "provider_tool_rejection.txt").read_text(encoding="utf-8").strip()

# #944's, one level deep: the embedding model that was routed a chat completion.
EMBEDDING_PAYLOAD = (
    'litellm.BadRequestError: OpenrouterException - {"error":{"message":"qwen/qwen3-embedding-8b '
    "is an embedding model and cannot be used with the chat/completions endpoint. Use the "
    '/embeddings endpoint instead.","code":400}}\nLiteLLM Retried: 2 times'
)

# The account identifier that rode into the browser in #947's screenshot.
ACCOUNT_ID = "user_2abcDEF3ghiJKL4mnoPQR5stu"


class BadRequestError(Exception):
    """Stands in for litellm's — the classifier matches on the type *name*, not the class."""

    llm_provider = "openrouter"


class AuthenticationError(Exception):
    llm_provider = "openrouter"


class RateLimitError(Exception):
    llm_provider = "openrouter"


class ServiceUnavailableError(Exception):
    llm_provider = "openrouter"


# ── the redaction helper ─────────────────────────────────────────────────────────────────────


def test_the_deepest_readable_sentence_is_the_one_returned() -> None:
    # OpenRouter's own message is a placeholder ("Provider returned error"); the sentence the
    # operator needs is vLLM's, three escape levels down inside `metadata.raw`.
    assert readable_provider_message(NEXTBIT_PAYLOAD) == (
        '"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set'
    )


def test_a_single_level_payload_yields_its_message() -> None:
    assert readable_provider_message(EMBEDDING_PAYLOAD) == (
        "qwen/qwen3-embedding-8b is an embedding model and cannot be used with the "
        "chat/completions endpoint. Use the /embeddings endpoint instead."
    )


def test_nothing_readable_yields_nothing_at_all() -> None:
    # No `message` key, so no quotation — the caller falls back to its own sentence rather than
    # reaching for `str(exc)`.
    assert (
        readable_provider_message('{"code":400,"detail":[{"loc":["body"],"type":"value"}]}') is None
    )


@pytest.mark.parametrize(
    "candidate",
    [
        '{"error":{"code":400}}',  # structure
        'boom","user_id":"abc',  # JSON punctuation
        "request_id 7 failed",  # an identifying key by name
        "token f4c2b9d81e0a4f7cb63d25ae99f01c47 rejected",  # a long mixed id run
        "short",  # too short to be a sentence
        "nospaceshereatallwhichisnotasentence",  # not a sentence
        "x" * 400,  # a dump, not a message
    ],
)
def test_a_candidate_that_is_not_a_sentence_is_refused(candidate: str) -> None:
    assert is_plain_sentence(candidate) is False


def test_a_long_flag_name_is_not_mistaken_for_an_identifier() -> None:
    # `--enable-auto-tool-choice` is 25 characters of id-shaped text and is genuinely part of
    # the message; the digit requirement is what tells it apart from a key or a uuid.
    assert is_plain_sentence(
        '"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set'
    )


# ── classification ───────────────────────────────────────────────────────────────────────────


def test_a_provider_rejection_names_the_model_the_provider_and_the_reason() -> None:
    failure = classify_stream_failure(
        BadRequestError(NEXTBIT_PAYLOAD), model="openrouter/qwen/qwen3-8b"
    )
    assert failure.reason == "rejected"
    assert "openrouter/qwen/qwen3-8b" in failure.banner
    # The upstream OpenRouter routed to, not just "openrouter" — that is what the operator acts on.
    assert "NextBit" in failure.banner
    assert "tool choice requires --enable-auto-tool-choice" in failure.banner
    # The banner and the note a retained partial answer keeps are the same sentence, so the
    # reason survives into the persisted transcript rather than living only in the live banner.
    assert failure.note == failure.banner


def test_no_provider_payload_survives_into_the_banner() -> None:
    for payload in (NEXTBIT_PAYLOAD, EMBEDDING_PAYLOAD):
        failure = classify_stream_failure(BadRequestError(payload), model="openrouter/m")
        for leak in (ACCOUNT_ID, "user_id", "{", "}", "LiteLLM Retried", "OpenrouterException"):
            assert leak not in failure.banner
            assert leak not in failure.note


def test_the_embedding_rejection_reads_as_the_owner_saw_it() -> None:
    failure = classify_stream_failure(
        BadRequestError(EMBEDDING_PAYLOAD), model="openrouter/qwen/qwen3-embedding-8b"
    )
    assert "is an embedding model" in failure.banner
    assert failure.reason == "rejected"


def test_the_paused_signal_is_the_one_passthrough() -> None:
    # `stores/chat.ts` tests /paused/i against the detail to show the asleep card, so this text
    # is a contract. It passes through *by exception type* now — not because nothing matched.
    failure = classify_stream_failure(
        GatewayPausedError("LLM gateway is paused; no non-local model is available")
    )
    assert failure.reason == "paused"
    assert "paused" in failure.banner


def test_a_plain_runtime_error_saying_paused_no_longer_passes_through() -> None:
    # The old rule was "anything unmatched keeps its own text", which is how a provider payload
    # reached the browser. An untyped exception is now generic, whatever it happens to say.
    failure = classify_stream_failure(RuntimeError("paused"), model="m")
    assert failure.reason == "unknown"
    assert failure.banner != "paused"
    assert "RuntimeError" in failure.banner


def test_a_capability_refusal_renders_its_message_and_hint() -> None:
    failure = classify_stream_failure(
        ModelCapabilityError(
            model="openrouter/qwen/qwen3-embedding-8b",
            capability="chat",
            message="openrouter/qwen/qwen3-embedding-8b is an embedding model, not a chat model",
            hint="Pick a chat model on the Models page",
        ),
        model="openrouter/qwen/qwen3-embedding-8b",
    )
    assert failure.reason == "capability"
    assert "is an embedding model, not a chat model" in failure.banner
    assert "Pick a chat model on the Models page" in failure.banner


def test_the_connection_class_keeps_its_wording() -> None:
    failure = classify_stream_failure(
        RuntimeError("litellm.APIConnectionError: Timeout on reading data from socket")
    )
    assert failure.reason == "stalled"
    assert failure.banner == STREAM_STALLED_MESSAGE
    assert failure.note == STREAM_STALLED_MESSAGE


@pytest.mark.parametrize(
    ("exc", "reason", "expected"),
    [
        (AuthenticationError("401 unauthorized"), "auth", "credentials"),
        (RateLimitError("429 slow down"), "rate_limited", "rate-limited"),
        (ServiceUnavailableError("503 upstream down"), "unavailable", "unavailable"),
    ],
)
def test_each_provider_class_gets_its_own_sentence(
    exc: Exception, reason: str, expected: str
) -> None:
    failure = classify_stream_failure(exc, model="openrouter/m")
    assert failure.reason == reason
    assert expected in failure.banner
    assert "openrouter/m" in failure.banner


def test_a_missing_model_still_reads_as_a_sentence() -> None:
    failure = classify_stream_failure(RateLimitError("429"))
    assert failure.banner.startswith("The model is rate-limited")
