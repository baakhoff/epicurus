"""The module-facing **platform API** (module → core), versioned under ``/platform/v1``.

Modules reach core capabilities — secrets, events, storage, the agent / LLM gateway,
the tool registry — through this local-only API (ADR-0004), rather than wiring to the
backends themselves.  Modules use the typed ``PlatformClient`` from ``epicurus_core``
to call these endpoints without holding provider credentials or SDK dependencies
(ADR-0010).

Endpoints
---------
GET  /platform/v1/info   — discovery: contract, core-app + library versions, release
                           track, tenant.
POST /platform/v1/embed  — embed texts via the LLM gateway (returns float vectors).
POST /platform/v1/chat   — chat completion via the LLM gateway.
"""

from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from epicurus_core import CONTRACT_VERSION, __version__
from epicurus_core_app.llm.gateway import LlmGateway
from epicurus_core_app.llm.models import ChatMessage, ChatResult
from epicurus_core_app.llm.prefs import LlmPrefsStore
from epicurus_core_app.settings import CoreAppSettings

# Vision gating for the module-facing chat path (#739). The interactive agent turn has gated
# image input since #633; this endpoint did not — so a module sending image content-parts to a
# model without vision got either a silent ignore or a raw provider error, the two outcomes
# that gate exists to prevent. The wording mirrors `agent.agent._VISION_UNSUPPORTED_MESSAGE`,
# put in the third person: the caller here is a module, which relays the text to the operator
# rather than speaking it itself.
UNSUPPORTED_MEDIA = "unsupported_media"
VISION_UNSUPPORTED_MESSAGE = (
    "The selected model can't see images — switch to a vision-capable model to send image content."
)
# ``image_url`` is the OpenAI-style part LiteLLM canonicalises on (and what the agent's own
# ``_attach_images`` emits); ``image`` is the Anthropic-native spelling. Both are recognised so
# the gate cannot be sidestepped by picking the other shape.
_IMAGE_PART_TYPES = frozenset({"image_url", "image"})


RELEASE_TRACK_ENV = "EPICURUS_VERSION"
"""The env var naming the image tag this deployment pulled (``latest`` / ``testing`` / a semver).

Read straight from the environment rather than through ``CoreAppSettings``: it is not a knob
the core acts on, it is the deployment's label for itself, and a setting with a default would
invent an answer where the honest one is "nothing said". Unset — a source checkout, a
`compose` file that does not pass it through — reports ``null``, which the card draws as an
em dash rather than as a claim.
"""


def _core_app_version() -> str:
    """The installed ``epicurus-core-app`` distribution version.

    The same read ``app._service_version`` makes for ``/health``, done here rather than
    imported: ``app`` imports this module, so the arrow only points one way. Both read the
    package metadata — the single source of truth #854 established — so they cannot drift.
    """
    try:
        return pkg_version("epicurus-core-app")
    except PackageNotFoundError:
        return "0.0.0"


def _carries_image(messages: list[ChatMessage]) -> bool:
    """Whether any message's content is a parts array holding an image part."""
    for message in messages:
        content = message.content
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") in _IMAGE_PART_TYPES:
                return True
    return False


async def _named_model(gateway: LlmGateway, model: str | None, tenant_id: str | None) -> str:
    """The model to *name* in the refusal — the request's override, else the core's default.

    Never raises: it runs only on the refusal path, and a store hiccup while resolving the
    default must not turn a clean 400 into a 500. An empty string means "couldn't say".
    """
    if model:
        return model
    try:
        return await gateway.effective_default(tenant_id)
    except Exception:
        return ""


class PlatformInfo(BaseModel):
    """What a module — and the Settings **Platform** card — learns about this core.

    ``core_version`` has always carried the *library* (``epicurus_core``) version while being
    labelled the core's, which is how the card came to show ``0.37.0`` as "core version" for a
    ``0.121.0`` core-app (#893). It is **kept, unchanged**: it is part of the module↔core
    contract, an installed module may read it, and renaming a published field to fix a *label*
    would break callers to no one's benefit. The two unambiguous fields are added beside it —
    ``core_app_version`` (the running service) and ``library_version`` (the same value
    ``core_version`` has always had) — and the card renders those.

    ``release_track`` is the deployment's own answer to "which build is this?": the
    ``EPICURUS_VERSION`` tag the operator pinned (``latest``, ``testing``, ``0.2.0``), or
    ``None`` where nothing set it. It is not derived from a version number — a semver says
    what the code claims to be, a track says what was actually pulled, and after a
    reconcile-that-did-not-take those are exactly the two facts that disagree.
    """

    contract_version: str
    core_version: str
    core_app_version: str
    library_version: str
    release_track: str | None = None
    tenant: str


class EmbedRequest(BaseModel):
    """Request body for ``POST /platform/v1/embed``."""

    texts: list[str]
    model: str | None = None
    tenant_id: str | None = None


class EmbedResponse(BaseModel):
    """Embedding vectors — one per input text."""

    embeddings: list[list[float]]


class PlatformChatRequest(BaseModel):
    """Request body for ``POST /platform/v1/chat``."""

    messages: list[ChatMessage]
    model: str | None = None
    tools: list[dict[str, Any]] | None = None
    tenant_id: str | None = None


def create_platform_router(
    settings: CoreAppSettings,
    gateway: LlmGateway,
    *,
    prefs: LlmPrefsStore | None = None,
    default_tenant: str = "local",
) -> APIRouter:
    """Build the ``/platform/v1`` router that modules call into."""
    router = APIRouter(prefix="/platform/v1", tags=["platform"])

    @router.get("/info", response_model=PlatformInfo)
    def info() -> PlatformInfo:
        track = os.environ.get(RELEASE_TRACK_ENV, "").strip()
        return PlatformInfo(
            contract_version=CONTRACT_VERSION,
            core_version=__version__,
            core_app_version=_core_app_version(),
            library_version=__version__,
            release_track=track or None,
            tenant=settings.default_tenant_id,
        )

    @router.post("/embed", response_model=EmbedResponse)
    async def embed(request: EmbedRequest) -> EmbedResponse:
        """Embed texts via the core's LLM gateway.

        Resolution order: per-module override (request.model) → global embedding
        default pref → env default (memory_embed_model).  Keys never leave the
        core; usage is metered via NATS.
        """
        tenant = request.tenant_id or default_tenant
        global_embed = await prefs.get_embed_default(tenant) if prefs is not None else None
        model = request.model or global_embed or settings.memory_embed_model
        embeddings = await gateway.embed(request.texts, model=model, tenant_id=request.tenant_id)
        return EmbedResponse(embeddings=embeddings)

    @router.post("/chat", response_model=ChatResult)
    async def chat(request: PlatformChatRequest) -> ChatResult:
        """Chat completion via the core's LLM gateway.

        The single module-facing chat path (ADR-0021): the core owns model
        selection, fallback, key management, and usage accounting — the module
        provides only messages and optional overrides. Returns the shared
        ``ChatResult``.

        **Vision gate (#739).** A request carrying image content-parts is refused
        with **400** when the resolved model has no vision support, before any
        provider call — the same rule the interactive agent turn has applied since
        #633, now on the module path too. The body is a structured detail the
        caller can branch on::

            {"detail": {"error": "unsupported_media",
                        "message": "...", "model": "ollama_chat/llama3.2"}}

        so a module can degrade honestly (return what it *did* extract, plus a note
        saying the caption was skipped and why) rather than guess from a provider
        error string. A text-only request is untouched.
        """
        if _carries_image(request.messages) and not await gateway.supports_vision(
            request.model, request.tenant_id
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": UNSUPPORTED_MEDIA,
                    "message": VISION_UNSUPPORTED_MESSAGE,
                    "model": await _named_model(gateway, request.model, request.tenant_id),
                },
            )
        return await gateway.chat(
            request.messages,
            model=request.model,
            tools=request.tools,
            tenant_id=request.tenant_id,
        )

    return router
