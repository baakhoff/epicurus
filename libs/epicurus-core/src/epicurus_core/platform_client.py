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

import time
from collections.abc import Callable
from typing import Any

import httpx

# The chat shapes are the shared contract (ADR-0021). ``PlatformMessage`` and
# ``PlatformChatResponse`` are backward-compatible aliases of ``ChatMessage`` /
# ``ChatResult`` — re-exported here so existing
# ``from epicurus_core.platform_client import PlatformChatResponse`` keeps resolving.
from epicurus_core.contracts import (
    CollectionPrefs,
    PlatformChatResponse,
    PlatformMessage,
    WebSearchResult,
)
from epicurus_core.files import FileEntry
from epicurus_core.logging import get_logger

__all__ = [
    "ModuleConfigCache",
    "PlatformChatResponse",
    "PlatformClient",
    "PlatformError",
    "PlatformMessage",
]


class PlatformError(Exception):
    """A refusal the core *explained*: a non-2xx answer whose body carries a ``code``.

    Raised by the platform calls whose contract is a structured error detail
    (``{"detail": {"code": ..., "message": ...}}``) — today :meth:`PlatformClient.web_search`.
    ``code`` is what a module branches on (``openrouter_key_missing`` and so on), ``message``
    is the operator-readable sentence to relay, and ``status`` the HTTP status the core used.

    An *anticipated* tool failure (ADR-0136): the tool-error seam logs it at WARNING and
    carries ``message`` to the model, exactly as it does an ``httpx.HTTPStatusError``.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _platform_error(resp: httpx.Response) -> PlatformError:
    """Build a :class:`PlatformError` from a non-2xx core answer, whatever its body.

    A structured ``detail`` object gives its ``code`` and ``message``; a plain-string detail
    (FastAPI's own 422, a proxy's page) keeps the text under the code ``http_<status>``, so
    the caller never loses the reason even when the core did not shape it.
    """
    code = f"http_{resp.status_code}"
    message = resp.reason_phrase or f"HTTP {resp.status_code}"
    try:
        body = resp.json()
    except ValueError:
        return PlatformError(resp.status_code, code, message)
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        code = str(detail.get("code") or code)
        message = str(detail.get("message") or message)
    elif isinstance(detail, str) and detail:
        message = detail
    return PlatformError(resp.status_code, code, message)


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

    async def get_timezone(self) -> str:
        """The operator's configured IANA timezone (ADR-0039), e.g. ``"Europe/Belgrade"``.

        Modules that accept naive (offset-less) date/time inputs — natural-language
        event or task times — resolve them in this zone rather than assuming UTC, so
        "3 PM" means the operator's 3 PM (#433). Raises ``httpx.HTTPStatusError`` /
        ``httpx.HTTPError`` when the core is unreachable; callers should degrade to UTC.
        """
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as http:
            resp = await http.get(
                "/platform/v1/timezone",
                params={"tenant_id": self._tenant_id},
            )
            resp.raise_for_status()
            return str(resp.json()["timezone"])

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

    async def get_module_config(self) -> dict[str, Any]:
        """This module's stored settings — what the operator saved on the Modules page (#984).

        The values the shell's settings form writes (``PUT /platform/v1/modules/{name}/config``,
        kept per tenant in OpenBao) and that, before #984, no module ever read back. Returns
        ``{}`` when nothing has been saved. Most callers want :class:`ModuleConfigCache`, which
        wraps this with a short cache and a last-known-good fallback. Requires
        ``PlatformClient(..., module=...)``.

        Raises:
            httpx.HTTPError: the core is unreachable or answered non-2xx.
        """
        if self._module is None:
            raise ValueError("PlatformClient.module must be set to read the module's settings")
        async with httpx.AsyncClient(base_url=self._base_url, timeout=10.0) as http:
            resp = await http.get(
                f"/platform/v1/modules/{self._module}/config",
                params={"tenant_id": self._tenant_id},
            )
            resp.raise_for_status()
            body = resp.json()
            return dict(body) if isinstance(body, dict) else {}

    async def web_search(self, query: str, *, max_results: int = 5) -> WebSearchResult:
        """Run one web search through the core's hosted search provider (#984).

        The core holds the provider key (constraints #4 and #8) — today the tenant's stored
        OpenRouter key — runs the search, meters it under this client's tenant, and returns
        normalised results. The module never sees the key.

        Raises:
            PlatformError: the core refused or the provider failed, with the core's ``code``:
                ``openrouter_key_missing`` (409, no key stored), ``openrouter_key_rejected``,
                ``provider_error``, ``provider_unreachable`` (502), ``key_store_unavailable``
                (503).
            httpx.TransportError: the core itself could not be reached.
        """
        payload: dict[str, Any] = {
            "query": query,
            "max_results": max_results,
            "tenant_id": self._tenant_id,
        }
        # A hosted search is a model call plus a search round trip; give it the chat budget.
        async with httpx.AsyncClient(base_url=self._base_url, timeout=120.0) as http:
            resp = await http.post("/platform/v1/web-search", json=payload)
            if resp.is_error:
                raise _platform_error(resp)
            return WebSearchResult.model_validate(resp.json())

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


class ModuleConfigCache:
    """A module's stored settings, read through the core with a short cache (#984).

    The Modules page saves a module's settings in the core; this is how the module *sees*
    them at runtime without a restart and without asking the core on every tool call. A read
    within ``ttl_s`` of the last successful fetch answers from memory. When the core cannot be
    reached the last good answer is kept (or ``{}`` if there has never been one) and the
    failure is logged once per outage, so a core restart never turns into a failing tool — the
    module falls back to its env defaults, which is what ``{}`` means to every caller.
    """

    def __init__(
        self,
        client: PlatformClient,
        *,
        ttl_s: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl_s = ttl_s
        self._clock = clock
        self._values: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._failing = False

    async def get(self) -> dict[str, Any]:
        """The stored settings — fresh within ``ttl_s``, else re-read (last-good on failure)."""
        now = self._clock()
        if self._fetched_at is not None and now - self._fetched_at < self._ttl_s:
            return dict(self._values)
        try:
            values = await self._client.get_module_config()
        except httpx.HTTPError as exc:
            if not self._failing:
                get_logger(__name__).warning(
                    "could not read the module's stored settings; using the last known",
                    error=str(exc),
                    have_last_known=self._fetched_at is not None,
                )
            self._failing = True
            return dict(self._values)
        self._failing = False
        self._values = values
        self._fetched_at = now
        return dict(values)

    def invalidate(self) -> None:
        """Forget the cached answer so the next :meth:`get` asks the core."""
        self._fetched_at = None
