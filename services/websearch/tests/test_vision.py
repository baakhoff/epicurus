"""Tests for image captioning through the core (vision.py, #739).

The captioner is the module's only inference path, and every one of its failure modes has
to be a *degrade*: a note the operator can act on, never an exception that ends the turn.
The case that matters most is the core's structured 400 (``unsupported_media``) — the gate
added to ``POST /platform/v1/chat`` in the same change — because that one is a settings
problem with an obvious fix, and the note has to say so.

The core is a ``MockTransport``-backed ``PlatformClient``, so nothing here needs a running
core or a configured model.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from epicurus_core import PlatformClient
from epicurus_websearch.vision import (
    Caption,
    VisionCaptioner,
    data_url,
    image_message,
)

PNG_B64 = "iVBORw0KGgoAAAANSUhEUg=="


class _RecordingClient(PlatformClient):
    """A PlatformClient whose chat call is served by a handler instead of the network."""

    def __init__(self, handler: Any) -> None:
        super().__init__(base_url="http://core-app:8080", tenant_id="local", module="websearch")
        self._handler = handler
        self.payloads: list[dict[str, Any]] = []

    async def chat(self, messages: Any, *, model: Any = None, tools: Any = None) -> Any:
        payload = {
            "messages": [m.model_dump(exclude_none=True) for m in messages],
            "model": model,
        }
        self.payloads.append(payload)
        transport = httpx.MockTransport(self._handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://core-app") as http:
            resp = await http.post("/platform/v1/chat", json=payload)
            resp.raise_for_status()
            from epicurus_core.contracts import PlatformChatResponse

            return PlatformChatResponse.model_validate(resp.json())


def _ok(content: str) -> Any:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "vision/m", "content": content})

    return handler


def _error(status: int, body: dict[str, Any]) -> Any:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return handler


# ── content parts ──────────────────────────────────────────────────────────────────────


def test_data_url_shape() -> None:
    assert data_url("image/png", "AAA") == "data:image/png;base64,AAA"


def test_image_message_builds_openai_style_content_parts() -> None:
    """The wire contract already allows this (``ChatMessage.content: str | list[dict]``)."""
    message = image_message("describe", mime="image/jpeg", data_b64=PNG_B64)
    assert message.role == "user"
    parts = message.content
    assert isinstance(parts, list)
    assert parts[0] == {"type": "text", "text": "describe"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_image_message_survives_the_platform_client_serialisation() -> None:
    """``model_dump(exclude_none=True)`` must not flatten or drop the parts array."""
    message = image_message("describe", mime="image/png", data_b64=PNG_B64)
    dumped = message.model_dump(exclude_none=True)
    assert isinstance(dumped["content"], list)
    assert dumped["content"][1]["type"] == "image_url"


# ── happy path ─────────────────────────────────────────────────────────────────────────


async def test_caption_returns_the_models_description() -> None:
    captioner = VisionCaptioner(_RecordingClient(_ok("  A red bicycle against a wall.  ")))
    caption = await captioner.caption(mime="image/png", data_b64=PNG_B64)
    assert caption == Caption(text="A red bicycle against a wall.")


async def test_caption_forwards_the_configured_model_override() -> None:
    client = _RecordingClient(_ok("ok"))
    captioner = VisionCaptioner(client, model="ollama_chat/llava")
    await captioner.caption(mime="image/png", data_b64=PNG_B64)
    assert client.payloads[0]["model"] == "ollama_chat/llava"


async def test_empty_model_setting_means_let_the_core_choose() -> None:
    client = _RecordingClient(_ok("ok"))
    await VisionCaptioner(client, model="").caption(mime="image/png", data_b64=PNG_B64)
    assert client.payloads[0]["model"] is None


# ── degrades ───────────────────────────────────────────────────────────────────────────


async def test_unsupported_media_400_degrades_with_an_actionable_note() -> None:
    """The #739 vision gate: the note names the model and the fix, not an HTTP status."""
    captioner = VisionCaptioner(
        _RecordingClient(
            _error(
                400,
                {
                    "detail": {
                        "error": "unsupported_media",
                        "message": "The selected model can't see images — switch to a"
                        " vision-capable model to send image content.",
                        "model": "ollama_chat/llama3.2",
                    }
                },
            )
        )
    )
    caption = await captioner.caption(mime="image/png", data_b64=PNG_B64)
    assert caption.text == ""
    assert "ollama_chat/llama3.2" in caption.note
    assert "vision-capable model" in caption.note


async def test_paused_gateway_degrades_with_its_own_note() -> None:
    captioner = VisionCaptioner(_RecordingClient(_error(503, {"detail": "gateway paused"})))
    caption = await captioner.caption(mime="image/png", data_b64=PNG_B64)
    assert caption.text == ""
    assert "paused" in caption.note


async def test_other_core_errors_degrade_with_the_status() -> None:
    captioner = VisionCaptioner(_RecordingClient(_error(500, {"detail": "boom"})))
    caption = await captioner.caption(mime="image/png", data_b64=PNG_B64)
    assert caption.text == ""
    assert "HTTP 500" in caption.note


async def test_a_non_json_error_body_still_degrades_cleanly() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"<html>bad gateway</html>")

    caption = await VisionCaptioner(_RecordingClient(handler)).caption(
        mime="image/png", data_b64=PNG_B64
    )
    assert caption.text == ""
    assert "HTTP 502" in caption.note


async def test_unreachable_core_degrades_rather_than_raising() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to core")

    caption = await VisionCaptioner(_RecordingClient(handler)).caption(
        mime="image/png", data_b64=PNG_B64
    )
    assert caption.text == ""
    assert "unreachable" in caption.note


async def test_an_empty_model_reply_is_reported_not_returned_as_a_caption() -> None:
    caption = await VisionCaptioner(_RecordingClient(_ok("   "))).caption(
        mime="image/png", data_b64=PNG_B64
    )
    assert caption.text == ""
    assert "no description" in caption.note


async def test_no_platform_client_means_captioning_is_unavailable() -> None:
    captioner = VisionCaptioner(None)
    assert captioner.available is False
    caption = await captioner.caption(mime="image/png", data_b64=PNG_B64)
    assert caption.text == ""
    assert "unavailable" in caption.note


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 500, 503])
async def test_no_core_status_ever_raises_out_of_caption(status: int) -> None:
    """The blanket guarantee: an ingest that got the article must not fail over an image."""
    caption = await VisionCaptioner(_RecordingClient(_error(status, {"detail": "x"}))).caption(
        mime="image/png", data_b64=PNG_B64
    )
    assert caption.note
