"""Hosted web search through OpenRouter, run by the core on a module's behalf (#984).

The websearch module can send a search here instead of to its bundled SearXNG once the
operator has chosen OpenRouter on the Modules page. The core runs it because only the core
holds provider keys (constraints #4 and #8): the tenant's OpenRouter key is read from OpenBao
at call time, sent to OpenRouter, and never logged or returned.

**The mechanism (checked against OpenRouter's docs, 2026-09).** OpenRouter has no search-only
endpoint; web search is a capability of a chat completion. The documented, current way to ask
for it is the ``openrouter:web_search`` *server tool* — ``tools: [{"type":
"openrouter:web_search", "parameters": {...}}]`` on ``POST /api/v1/chat/completions`` — which
replaced the ``web`` plugin and the ``:online`` model suffix (both documented as deprecated).
OpenRouter executes the tool itself and surfaces what it found to the caller as
``url_citation`` annotations on the assistant message (``url``, ``title``, ``content``), and
counts the searches it ran in ``usage.server_tool_use.web_search_requests``. So one search is a
completion against a small, cheap model (``OPENROUTER_WEB_SEARCH_MODEL``) told to search once
for the query, with ``max_uses: 1`` capping it at one search; the annotations are the results
and the model's own prose is thrown away.

Called with raw ``httpx`` rather than LiteLLM on purpose: a server tool is an OpenRouter-only
tool *type*, and the annotations are an OpenRouter-only response field — the two things this
feature is made of are exactly what a provider-agnostic layer normalises away.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from epicurus_core import (
    EventBus,
    SecretError,
    SecretNotFoundError,
    SecretStore,
    WebSearchHit,
    WebSearchResult,
    get_logger,
)
from epicurus_core_app.llm import providers as registry
from epicurus_core_app.llm.gateway import USAGE_SUBJECT
from epicurus_core_app.llm.models import UsageEvent

log = get_logger("epicurus_core_app.llm.web_search")

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
BACKEND = "openrouter"
# What a result's hover-card names as its engine: who found it, as the operator knows them.
ENGINE_LABEL = "OpenRouter"

# ``detail.code`` values of the endpoint's structured errors — the module branches on these.
KEY_MISSING = "openrouter_key_missing"
KEY_REJECTED = "openrouter_key_rejected"
KEY_STORE_UNAVAILABLE = "key_store_unavailable"
PROVIDER_ERROR = "provider_error"
PROVIDER_UNREACHABLE = "provider_unreachable"

# A result's snippet is a search *preview*, not the page: OpenRouter hands back ~2-4k characters
# of extracted page text per citation, which is far more than a chips list or a tool message
# should carry. ``link_ingest`` is how the agent reads the page itself.
_SNIPPET_CHARS = 400
# The provider's own error text is surfaced (the operator needs it: "insufficient credits",
# "model not found"), but bounded — it is untrusted input on its way into a log and a tool reply.
_PROVIDER_MESSAGE_CHARS = 300

_SYSTEM_PROMPT = (
    "You are a search relay. Call the web search tool exactly once, with the user's message as"
    " the query, word for word. After the search, reply with the single word: done."
)

_WHITESPACE = re.compile(r"\s+")


class WebSearchError(Exception):
    """A search the core could not run, with the structured detail the endpoint answers with."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "backend": BACKEND}


def _snippet(text: str) -> str:
    flat = _WHITESPACE.sub(" ", text).strip()
    if len(flat) <= _SNIPPET_CHARS:
        return flat
    return flat[: _SNIPPET_CHARS - 1].rstrip() + "…"


def _json_or_none(resp: httpx.Response) -> Any:
    """The response's JSON body, or ``None`` when it is not JSON."""
    try:
        return resp.json()
    except ValueError:
        return None


def _provider_message(resp: httpx.Response) -> str:
    """OpenRouter's own explanation of a failure (``{"error": {"message": ...}}``), bounded."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    message = ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        elif isinstance(error, str):
            message = error
    message = _WHITESPACE.sub(" ", message).strip() or resp.reason_phrase or "no reason given"
    return message[:_PROVIDER_MESSAGE_CHARS]


def parse_citations(message: dict[str, Any], limit: int) -> list[WebSearchHit]:
    """The ``url_citation`` annotations on an assistant message, as normalised results.

    Accepts both documented shapes — the chat-completions nesting
    (``{"type": "url_citation", "url_citation": {"url", "title", "content"}}``) and the
    Responses API's flat one (``{"type": "url_citation", "url", "title", "content"}``) — keeps
    ``http(s)`` URLs only, drops repeat citations of the same page (a model that cites one
    source twice yields one result), and stops at ``limit``.
    """
    hits: list[WebSearchHit] = []
    seen: set[str] = set()
    for annotation in message.get("annotations") or []:
        if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
            continue
        inner = annotation.get("url_citation")
        citation = inner if isinstance(inner, dict) else annotation
        url = str(citation.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        title = str(citation.get("title") or "").strip() or urlsplit(url).netloc
        hits.append(
            WebSearchHit(
                title=title,
                url=url,
                snippet=_snippet(str(citation.get("content") or "")),
                engine=ENGINE_LABEL,
            )
        )
        if len(hits) >= limit:
            break
    return hits


def _searches_run(usage: dict[str, Any]) -> int | None:
    """``usage.server_tool_use.web_search_requests``, or ``None`` when OpenRouter did not say."""
    server_tool_use = usage.get("server_tool_use")
    if not isinstance(server_tool_use, dict):
        return None
    count = server_tool_use.get("web_search_requests")
    return count if isinstance(count, int) else None


class OpenRouterWebSearch:
    """Runs one web search through OpenRouter's ``openrouter:web_search`` server tool."""

    def __init__(
        self,
        *,
        secrets: SecretStore,
        bus: EventBus,
        default_tenant: str,
        model: str,
        engine: str = "auto",
        timeout: float = 60.0,
        api_base: str = OPENROUTER_API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._secrets = secrets
        self._bus = bus
        self._default_tenant = default_tenant
        self._model = model
        self._engine = engine
        self._timeout = timeout
        self._api_base = api_base.rstrip("/")
        # Tests hand in an ``httpx.MockTransport``; production uses the default network one.
        self._transport = transport

    @property
    def model(self) -> str:
        return self._model

    @property
    def engine(self) -> str:
        return self._engine

    async def _api_key(self, tenant: str) -> str:
        secret_path = registry.PROVIDERS[BACKEND].secret_path or "llm/openrouter"
        try:
            secret = await self._secrets.get(secret_path, tenant)
        except SecretNotFoundError as exc:
            raise WebSearchError(
                409,
                KEY_MISSING,
                "No OpenRouter API key is stored. Add one on the Models page, or switch web"
                " search back to SearXNG in the websearch module's settings.",
            ) from exc
        except SecretError as exc:
            # "Could not ask" is not "there is no key" (#728): say which one it is.
            raise WebSearchError(
                503,
                KEY_STORE_UNAVAILABLE,
                "The core could not read the OpenRouter key from its secret store right now.",
            ) from exc
        key = str(secret.get("api_key") or "")
        if not key:
            raise WebSearchError(409, KEY_MISSING, "The stored OpenRouter API key is empty.")
        return key

    def _request_body(self, query: str, max_results: int) -> dict[str, Any]:
        parameters: dict[str, Any] = {"max_results": max_results, "max_uses": 1}
        if self._engine and self._engine != "auto":
            parameters["engine"] = self._engine
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            "tools": [{"type": "openrouter:web_search", "parameters": parameters}],
            # The reply is discarded; only the annotations matter. Keep the prose short.
            "max_tokens": 64,
        }

    async def search(
        self, query: str, *, max_results: int = 5, tenant_id: str | None = None
    ) -> WebSearchResult:
        """Search the web for ``query`` with the tenant's OpenRouter key.

        Raises :class:`WebSearchError` — never returns a silent empty result for a failure:
        409 ``openrouter_key_missing`` without a stored key; 503 ``key_store_unavailable`` when
        OpenBao cannot be asked; 502 ``openrouter_key_rejected`` for a 401/403 from OpenRouter,
        ``provider_error`` for any other non-2xx or an unreadable body, and
        ``provider_unreachable`` for a timeout or a failed connection.
        """
        tenant = tenant_id or self._default_tenant
        api_key = await self._api_key(tenant)
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as http:
                resp = await http.post(
                    f"{self._api_base}/chat/completions",
                    json=self._request_body(query, max_results),
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        # OpenRouter's documented app-attribution headers; optional, harmless.
                        "HTTP-Referer": "https://github.com/baakhoff/epicurus",
                        "X-Title": "epicurus",
                    },
                )
        except httpx.TransportError as exc:
            log.warning("openrouter web search unreachable", error=type(exc).__name__)
            raise WebSearchError(
                502,
                PROVIDER_UNREACHABLE,
                f"OpenRouter could not be reached ({type(exc).__name__}).",
            ) from exc
        latency_ms = (time.monotonic() - start) * 1000
        if resp.status_code in (401, 403):
            raise WebSearchError(
                502,
                KEY_REJECTED,
                f"OpenRouter rejected the stored API key ({resp.status_code}):"
                f" {_provider_message(resp)}. Replace the key on the Models page.",
            )
        if resp.is_error:
            message = _provider_message(resp)
            log.warning(
                "openrouter web search failed", status=resp.status_code, provider_message=message
            )
            raise WebSearchError(
                502,
                PROVIDER_ERROR,
                f"OpenRouter web search failed ({resp.status_code}): {message}",
            )
        data = _json_or_none(resp)
        if isinstance(data, dict) and data.get("error"):
            # OpenRouter can answer 200 with an ``error`` object (an upstream failure after the
            # status line was sent). That is a failure, not an empty search.
            raise WebSearchError(
                502, PROVIDER_ERROR, f"OpenRouter web search failed: {_provider_message(resp)}"
            )
        choices = data.get("choices") if isinstance(data, dict) else None
        first = choices[0] if isinstance(choices, list) and choices else None
        message_body = first.get("message") if isinstance(first, dict) else None
        if not isinstance(data, dict) or not isinstance(message_body, dict):
            raise WebSearchError(
                502, PROVIDER_ERROR, "OpenRouter answered without a readable completion."
            )
        raw_usage = data.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        searches = _searches_run(usage)
        hits = parse_citations(message_body, max_results)
        await self._emit_usage(
            model=str(data.get("model") or self._model),
            usage=usage,
            searches=searches,
            latency_ms=latency_ms,
            tenant=tenant,
        )
        # Citations prove a search ran even if the usage block is silent about it.
        searched: bool | None = True if hits else (None if searches is None else searches > 0)
        return WebSearchResult(
            results=hits,
            searched=searched,
            backend=BACKEND,
            model=self._model,
            search_engine=self._engine,
        )

    async def _emit_usage(
        self,
        *,
        model: str,
        usage: dict[str, Any],
        searches: int | None,
        latency_ms: float,
        tenant: str,
    ) -> None:
        """Meter the call under the caller's tenant (constraint #1). Best-effort, like chat's."""
        event = UsageEvent(
            model=f"{BACKEND}/{model}",
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            latency_ms=round(latency_ms),
            tenant=tenant,
            web_search_requests=searches,
        )
        try:
            await self._bus.publish(USAGE_SUBJECT, event.model_dump(), tenant_id=tenant)
        except Exception:  # metering must never break the search
            log.warning("usage event publish failed", exc_info=True)
