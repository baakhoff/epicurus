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
from epicurus_core.contracts import CollectionPrefs, PlatformChatResponse, PlatformMessage
from epicurus_core.files import FileEntry

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

    def __init__(self, base_url: str, tenant_id: str, *, module: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._tenant_id = tenant_id
        # The module's own name — needed only by ``get_module_model`` (#128). Callers that
        # use embed / chat / oauth need not set it.
        self._module = module

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

    async def get_module_model(self, slot: str) -> str | None:
        """The operator's chosen model for one of this module's slots, or ``None`` (#128).

        The module declares model slots in its manifest (``required_models``); the operator
        picks a model per slot in the shell. Returns the selected model id, or ``None`` when
        the slot is unset — pass the result straight to :meth:`embed` / :meth:`chat`: a model
        means "use this", ``None`` means "let the core pick its default".

        Requires the client to know its module name (``PlatformClient(..., module=...)``).

        Args:
            slot: A slot key from the module's ``required_models`` (e.g. ``"embedding"``).
        """
        if self._module is None:
            raise ValueError("PlatformClient.module must be set to resolve a model slot")
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as http:
            resp = await http.get(
                f"/platform/v1/modules/{self._module}/models/{slot}",
                params={"tenant_id": self._tenant_id},
            )
            resp.raise_for_status()
            model = resp.json().get("model")
            return str(model) if model else None

    async def get_suggestions_enabled(self) -> bool:
        """Whether this module's agent changes go through review (default ``True``).

        Read straight from the core's Postgres (no manifest round-trip). When ``False`` the
        operator has turned review off, so the module should apply the agent's change
        directly instead of staging a suggestion. Requires ``PlatformClient(..., module=...)``.
        """
        if self._module is None:
            raise ValueError("PlatformClient.module must be set to resolve suggestions setting")
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as http:
            resp = await http.get(
                f"/platform/v1/modules/{self._module}/suggestions-enabled",
                params={"tenant_id": self._tenant_id},
            )
            resp.raise_for_status()
            return bool(resp.json().get("enabled", True))

    async def get_collections(self) -> CollectionPrefs:
        """The operator's collection selection for this module (ADR-0030).

        Returns the stored ``{enabled, active}`` read straight from the core's Postgres —
        **no module round-trip** — which the module uses to route reads/writes: an empty
        ``enabled`` and a null ``active`` both mean "use the local default". Requires the
        client to know its module name (``PlatformClient(..., module=...)``).
        """
        if self._module is None:
            raise ValueError("PlatformClient.module must be set to resolve collections")
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as http:
            resp = await http.get(
                f"/platform/v1/modules/{self._module}/collections/prefs",
                params={"tenant_id": self._tenant_id},
            )
            resp.raise_for_status()
            return CollectionPrefs.model_validate(resp.json())

    async def list_modules(self) -> list[dict[str, Any]]:
        """List all modules with their manifests and enabled states (#215).

        Returns a list of snapshot dicts, each with ``manifest`` (including ``docs_url``),
        ``enabled``, ``removed``, and ``status`` fields.
        """
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as http:
            resp = await http.get(
                "/platform/v1/modules",
                params={"tenant_id": self._tenant_id},
            )
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    async def get_module_docs(self, name: str) -> list[dict[str, Any]]:
        """Fetch the documentation pages a module declares (#215).

        The core proxies the module's ``docs_url`` endpoint and returns the parsed JSON.
        Each element is a ``{"path": str, "content": str}`` dict.

        Raises:
            httpx.HTTPStatusError: 404 when the module has no ``docs_url``; other
                non-2xx for connection or server errors.

        Args:
            name: Module name, e.g. ``"echo"``.
        """
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as http:
            resp = await http.get(
                f"/platform/v1/modules/{name}/docs",
                params={"tenant_id": self._tenant_id},
            )
            resp.raise_for_status()
            return resp.json()["documents"]  # type: ignore[no-any-return]

    # ── Core-owned file space (ADR-0052) ─────────────────────────────────────────
    # Modules consume the tenant file space through these instead of mounting /data and
    # doing their own I/O. The core owns the backend (local-FS ↔ S3) and tenant scoping.

    def _files_params(self, path: str) -> dict[str, str]:
        return {"path": path, "tenant_id": self._tenant_id}

    async def files_list(self, path: str = "") -> list[FileEntry]:
        """List the direct children of *path* in the tenant file space (empty = root)."""
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as http:
            resp = await http.get("/platform/v1/files/list", params=self._files_params(path))
            resp.raise_for_status()
            return [FileEntry.model_validate(e) for e in resp.json()["entries"]]

    async def files_search(self, query: str, *, limit: int = 50) -> list[FileEntry]:
        """Search the tenant file space by name/path fragment (the core-owned index).

        Case-insensitive; returns up to *limit* matching entries. Backs a module's agent
        file-search tool now that the core — not the module — owns the file index (ADR-0063).
        """
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as http:
            resp = await http.get(
                "/platform/v1/files/search",
                params={"q": query, "limit": str(limit), "tenant_id": self._tenant_id},
            )
            resp.raise_for_status()
            return [FileEntry.model_validate(e) for e in resp.json()["entries"]]

    async def files_read(self, path: str) -> str:
        """Read a UTF-8 text file from the tenant file space.

        Raises ``httpx.HTTPStatusError``: 404 (missing), 413 (too large), 415 (binary).
        """
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as http:
            resp = await http.get("/platform/v1/files/read", params=self._files_params(path))
            resp.raise_for_status()
            return str(resp.json()["content"])

    async def files_write(self, path: str, content: str) -> FileEntry:
        """Write UTF-8 *content* at *path* (creating parents); returns the stored entry."""
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as http:
            resp = await http.put(
                "/platform/v1/files/write",
                params=self._files_params(path),
                json={"content": content},
            )
            resp.raise_for_status()
            return FileEntry.model_validate(resp.json())

    async def files_stat(self, path: str) -> FileEntry | None:
        """Return the entry at *path*, or ``None`` if it does not exist."""
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as http:
            resp = await http.get("/platform/v1/files/stat", params=self._files_params(path))
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return FileEntry.model_validate(resp.json())

    async def files_delete(self, path: str) -> bool:
        """Delete the file or directory tree at *path*; returns whether it existed."""
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as http:
            resp = await http.request(
                "DELETE", "/platform/v1/files", params=self._files_params(path)
            )
            resp.raise_for_status()
            return bool(resp.json()["deleted"])

    async def files_make_dir(self, path: str) -> FileEntry:
        """Create the directory at *path* (and parents) if absent; returns its entry."""
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as http:
            resp = await http.post("/platform/v1/files/dir", params=self._files_params(path))
            resp.raise_for_status()
            return FileEntry.model_validate(resp.json())

    async def files_move(self, src: str, dst: str) -> FileEntry:
        """Move or rename *src* to *dst* in the tenant file space; returns the moved entry.

        Renaming is the same-parent case of moving. Raises ``httpx.HTTPStatusError``: 404
        (source missing), 409 (destination occupied), 400 (tenant-root or into-itself).
        """
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as http:
            resp = await http.post(
                "/platform/v1/files/move",
                params={"tenant_id": self._tenant_id},
                json={"src": src, "dst": dst},
            )
            resp.raise_for_status()
            return FileEntry.model_validate(resp.json())
