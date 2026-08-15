"""Describing an image through the core's LLM gateway (#739, constraint #8).

The module holds no model access and no keys: it builds OpenAI-style content parts and
posts them to ``POST /platform/v1/chat`` via ``PlatformClient``, and the core decides which
model runs, meters the usage, and — since #739 — **refuses the call outright** when the
resolved model cannot see images, with a structured 400 rather than a silent ignore or a
provider error.

Every failure here is a *degrade*, never a raised exception: an ingest that produced a
title, a byline, and 4,000 words of article is still a good result when the lead image went
undescribed.  What the caller gets back is a caption or a note explaining its absence, and
the note is written to be read by the operator — "no vision model is configured" is
actionable, "HTTPStatusError" is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from epicurus_core import PlatformClient, PlatformMessage, get_logger

log = get_logger("epicurus_websearch.vision")

# The 400 the core raises for an image sent to a model without vision (platform_api.py).
UNSUPPORTED_MEDIA = "unsupported_media"

DEFAULT_PROMPT = (
    "Describe this image for someone who cannot see it. Say what it shows, and transcribe"
    " any text that appears in it, verbatim. Be specific and factual; do not speculate about"
    " anything the image does not actually show. Two or three sentences."
)


@dataclass(frozen=True)
class Caption:
    """The outcome of one captioning attempt — at most one of the two is set.

    ``note`` is the operator-facing explanation when ``text`` is empty; it rides into the
    ingest result's ``notes`` so the agent can tell the operator what was skipped and why
    instead of quietly returning an image with no description.
    """

    text: str = ""
    note: str = ""


def data_url(mime: str, data_b64: str) -> str:
    """The ``data:`` URL an ``image_url`` content part carries."""
    return f"data:{mime};base64,{data_b64}"


def image_message(prompt: str, *, mime: str, data_b64: str) -> PlatformMessage:
    """One user message carrying ``prompt`` plus the image, as OpenAI-style content parts.

    ``ChatMessage.content`` already accepts ``str | list[dict] | None`` (the shape the agent's
    own ``_attach_images`` builds for #633), so the wire contract needs nothing new — the
    parts array goes through ``PlatformClient.chat`` unchanged and LiteLLM's provider
    adapters translate it per provider on the far side.
    """
    parts: list[dict[str, Any]] = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": data_url(mime, data_b64)}},
    ]
    return PlatformMessage(role="user", content=parts)


class VisionCaptioner:
    """Captions images through the core, degrading to a note on every failure path."""

    def __init__(
        self,
        platform: PlatformClient | None,
        *,
        model: str = "",
        prompt: str = DEFAULT_PROMPT,
    ) -> None:
        self._platform = platform
        self._model = model or None
        self._prompt = prompt

    @property
    def available(self) -> bool:
        """Whether a platform client is wired at all (it is not in a bare unit test)."""
        return self._platform is not None

    async def caption(self, *, mime: str, data_b64: str) -> Caption:
        """Describe one image, or explain why it could not be described."""
        if self._platform is None:
            return Caption(note="image descriptions are unavailable — the core is not reachable")
        message = image_message(self._prompt, mime=mime, data_b64=data_b64)
        try:
            result = await self._platform.chat([message], model=self._model)
        except httpx.HTTPStatusError as exc:
            return Caption(note=_note_for_status(exc))
        except httpx.HTTPError as exc:
            log.warning("vision caption request failed", error=str(exc))
            return Caption(note="the image could not be described — the core was unreachable")
        text = (result.content or "").strip()
        if not text:
            return Caption(note="the model returned no description for the image")
        return Caption(text=text)


def _note_for_status(exc: httpx.HTTPStatusError) -> str:
    """Turn the core's error into something the operator can act on.

    The one we care about is #739's structured 400: the model in use has no vision. That is a
    settings problem with an obvious fix, so the note says so and names the model, instead of
    leaving the operator to infer it from an empty ``image_descriptions``.
    """
    detail = _detail(exc.response)
    if exc.response.status_code == 400 and detail.get("error") == UNSUPPORTED_MEDIA:
        model = str(detail.get("model") or "").strip()
        named = f" ({model})" if model else ""
        return (
            f"the image was not described: the configured model{named} cannot see images —"
            " switch to a vision-capable model to get image descriptions"
        )
    if exc.response.status_code == 503:
        return "the image was not described: the core's LLM gateway is paused"
    log.warning(
        "vision caption rejected by the core",
        status=exc.response.status_code,
        detail=str(detail or exc.response.text)[:200],
    )
    return (
        "the image was not described: the core refused the request"
        f" (HTTP {exc.response.status_code})"
    )


def _detail(response: httpx.Response) -> dict[str, Any]:
    """The ``detail`` object from a FastAPI error body, or ``{}`` for anything else."""
    try:
        body = response.json()
    except ValueError:
        return {}
    if not isinstance(body, dict):
        return {}
    detail = body.get("detail")
    return detail if isinstance(detail, dict) else {}
