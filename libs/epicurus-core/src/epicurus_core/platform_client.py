"""Typed client that modules use to call the core platform API (module → core, ADR-0004).

A module imports ``PlatformClient`` from ``epicurus_core`` and calls
``embed`` / ``chat`` without holding provider SDK dependencies or API keys.
The core's LLM gateway (ADR-0010) owns model selection, key management,
fallback, and usage accounting.

Example::

    from epicurus_core import PlatformClient, PlatformMessage

    client = PlatformClient(
        base_url=settings.platform_url,
        tenant_id=settings.default_tenant_id,
    )
    embeddings = await client.embed(["text to index"])
    result = await client.chat(
        [PlatformMessage(role="user", content="summarise this")]
    )
"""

from __future__ import annotations

from typing import Any

import httpx

# The chat shapes are the shared contract (ADR-0021). ``PlatformMessage`` and
# ``PlatformChatResponse`` are backward-compatible aliases of ``ChatMessage`` /
# ``ChatResult`` — re-exported here so existing
# ``from epicurus_core.platform_client import PlatformChatResponse`` keeps resolving.
from epicurus_core.contracts import PlatformChatResponse, PlatformMessage

__all__ = ["PlatformChatResponse", "PlatformClient", "PlatformMessage"]


class PlatformClient:
    """Typed HTTP client for the module → core platform API (``/platform/v1``).

    Modules instantiate one client per service, scoped to their tenant.  The
    client never holds provider credentials — all inference requests are
    proxied through the core's LLM gateway.

    Args:
        base_url: Internal base URL of the core service, e.g.
            ``http://core:8080``.
        tenant_id: The tenant this module acts on behalf of.
    """

    def __init__(self, base_url: str, tenant_id: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._tenant_id = tenant_id

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
    ) -> list[list[float]]:
        """Embed *texts* via the core's LLM gateway.

        Returns one float vector per input text.  When *model* is omitted the
        core uses its configured default embedding model.

        Raises:
            httpx.HTTPStatusError: if the core returns a non-2xx status.
        """
        payload: dict[str, Any] = {"texts": texts, "tenant_id": self._tenant_id}
        if model is not None:
            payload["model"] = model
        async with httpx.AsyncClient(base_url=self._base_url, timeout=60.0) as http:
            resp = await http.post("/platform/v1/embed", json=payload)
            resp.raise_for_status()
            return resp.json()["embeddings"]  # type: ignore[no-any-return]

    async def chat(
        self,
        messages: list[PlatformMessage],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> PlatformChatResponse:
        """Chat completion via the core's LLM gateway.

        The core owns model selection, key management, fallback, and usage
        accounting — the module just supplies messages.

        Args:
            messages: The conversation so far.
            model: Override the model; the core picks a default when omitted.
            tools: OpenAI-format tool descriptors to enable tool calling.

        Raises:
            httpx.HTTPStatusError: if the core returns a non-2xx status
                (e.g. 503 when the gateway is paused).
        """
        payload: dict[str, Any] = {
            "messages": [m.model_dump(exclude_none=True) for m in messages],
            "tenant_id": self._tenant_id,
        }
        if model is not None:
            payload["model"] = model
        if tools is not None:
            payload["tools"] = tools
        async with httpx.AsyncClient(base_url=self._base_url, timeout=120.0) as http:
            resp = await http.post("/platform/v1/chat", json=payload)
            resp.raise_for_status()
            return PlatformChatResponse.model_validate(resp.json())

    async def get_oauth_token(self, provider: str) -> str:
        """Fetch a valid (auto-refreshed) OAuth access token for *provider*.

        The core owns the token vault and refresh logic — the module never sees
        a client secret or refresh token.  Raises ``httpx.HTTPStatusError``
        (404 or 400) when the provider is not connected for this tenant.

        Args:
            provider: Provider key, e.g. ``"google"``.

        Returns the raw access-token string, ready to use in
        ``Authorization: Bearer <token>``.
        """
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as http:
            resp = await http.get(
                f"/platform/v1/oauth/{provider}/token",
                params={"tenant_id": self._tenant_id},
            )
            resp.raise_for_status()
            return str(resp.json()["access_token"])
