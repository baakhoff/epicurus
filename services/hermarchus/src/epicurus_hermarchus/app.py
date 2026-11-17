"""The controller <-> Hermarchus HTTP contract (``/v1``), plus ``/health`` and ``/metrics``.

Everything under ``/v1`` needs ``Authorization: Bearer <HERMARCHUS_TOKEN>``. ``/health`` and
``/metrics`` are open, as on every other service in the stack (probes and Prometheus are
inside the boundary); neither carries a session id, a path, a title or a prompt.

A request may name a profile — ``X-Epicurus-Profile`` / ``X-Epicurus-Tenant`` header or a
``tenant_id`` query parameter — and if it names one, it must be the profile this instance is
bound to: anything else is a 403, a malformed one a 400.
"""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from epicurus_core.config import CoreSettings
from epicurus_core.logging import configure_logging, get_logger
from epicurus_core.observability import create_ops_router
from epicurus_core.tenancy import TenantError, validate_tenant_id
from epicurus_hermarchus import CONTRACT_VERSION, __version__
from epicurus_hermarchus.acp import PROTOCOL_VERSION
from epicurus_hermarchus.broker import Broker, Subscription
from epicurus_hermarchus.install import Installer
from epicurus_hermarchus.kinds import AgentKind, KindError, load_kinds
from epicurus_hermarchus.metrics import Metrics
from epicurus_hermarchus.runtime import (
    TERMINAL_EVENTS,
    ConflictError,
    LimitError,
    NotFoundError,
    SessionManager,
    UnavailableError,
)
from epicurus_hermarchus.sandbox import PathError
from epicurus_hermarchus.settings import HermarchusSettings, SettingsError
from epicurus_hermarchus.store import Event, Store

__all__ = ["create_app", "stream_events"]

log = get_logger("hermarchus")

_REPLAY_PAGE = 500
_KEEPALIVE_S = 15.0


# ── request bodies ─────────────────────────────────────────────────────────────
class CreateSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str = Field(min_length=1, max_length=64)
    # Relative to the workspace (or absolute inside it). Created when missing.
    cwd: str = Field(default=".", max_length=4096)
    title: str | None = Field(default=None, max_length=200)
    # Unset: the instance default (HERMARCHUS_UNATTENDED).
    unattended: bool | None = None


class SendInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1_000_000)
    mode: Literal["queue", "steer"] = "queue"


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["once", "always", "reject"]


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _sse(event: Event) -> bytes:
    data = json.dumps(event.as_dict(), separators=(",", ":"), ensure_ascii=False)
    return f"id: {event.seq}\nevent: {event.type}\ndata: {data}\n\n".encode()


def _kind_info(kind: AgentKind, path: str, installer: Installer) -> dict[str, Any]:
    reason = kind.unavailable_reason(path)
    install = installer.states.get(kind.kind)
    return {
        "kind": kind.kind,
        "name": kind.name,
        "builtin": kind.builtin,
        "verified": kind.verified,
        "command": kind.command,
        "args": list(kind.args),
        # Names only: a kind's env may carry an operator's credentials.
        "env": sorted(kind.env),
        "install_command": kind.install,
        "install": install.as_dict() if install else None,
        "docs": list(kind.docs),
        "available": reason is None,
        "reason": reason,
    }


async def stream_events(
    mgr: SessionManager,
    broker: Broker,
    session_id: str,
    *,
    after: int,
    follow: bool,
    queue_size: int,
) -> AsyncGenerator[bytes, None]:
    """A session's events as server-sent-event frames: replay after ``after``, then follow.

    Each frame is ``id: <seq>`` / ``event: <type>`` / ``data: <event JSON>``. A following
    stream ends after a terminal event, when the session is deleted, or — if this connection
    fell more than ``queue_size`` events behind — with a ``stream.overflow`` frame whose data
    is ``{"resume_after": <last delivered seq>}``.
    """
    # Subscribe *before* replaying, so nothing recorded during the replay is missed;
    # whatever the replay already covered is skipped by sequence number.
    sub: Subscription | None = broker.subscribe(session_id, queue_size) if follow else None
    last = after
    try:
        ended = False
        while True:
            page = await mgr.store.events_after(session_id, last, _REPLAY_PAGE)
            for event in page:
                yield _sse(event)
                last = event.seq
                ended = ended or event.type in TERMINAL_EVENTS
            if len(page) < _REPLAY_PAGE:
                break
        if sub is None or ended:
            return
        if session_id not in mgr.live:
            return  # nothing more will ever be recorded
        while True:
            if sub.overflowed:
                payload = json.dumps({"resume_after": last})
                yield f"event: stream.overflow\ndata: {payload}\n\n".encode()
                return
            if sub.closed:
                return
            try:
                item: Event | None = await asyncio.wait_for(sub.queue.get(), timeout=_KEEPALIVE_S)
            except TimeoutError:
                yield b": keepalive\n\n"
                continue
            if item is None:
                return
            event = item
            if event.seq <= last:
                continue
            if event.seq > last + 1:
                # Defensive: fill any gap from the durable log rather than skip it.
                for missed in await mgr.store.events_after(session_id, last, event.seq - last):
                    if missed.seq < event.seq:
                        yield _sse(missed)
                        last = missed.seq
            yield _sse(event)
            last = event.seq
            if event.type in TERMINAL_EVENTS:
                return
    finally:
        if sub is not None:
            broker.unsubscribe(sub)


def create_app(settings: HermarchusSettings) -> FastAPI:
    """Build the app. ``settings`` must have passed :meth:`HermarchusSettings.validated`."""
    try:
        kinds = load_kinds(settings.kinds_file)
    except KindError as exc:
        raise SettingsError(str(exc)) from exc
    token = settings.bearer_token().encode("utf-8")
    profile = settings.profile
    metrics = Metrics()
    broker = Broker(on_overflow=metrics.stream_overflows.inc)
    installer = Installer(settings, kinds)
    state: dict[str, Any] = {}

    def manager() -> SessionManager:
        mgr = state.get("manager")
        if mgr is None:
            raise _error(503, "starting", "Hermarchus is still starting")
        assert isinstance(mgr, SessionManager)
        return mgr

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        configure_logging(CoreSettings(service_name="hermarchus"))
        for directory in (settings.workspace_dir, settings.state_dir, settings.agent_home):
            directory.mkdir(parents=True, exist_ok=True)
        store = await Store.open(settings.database_path, profile)
        mgr = SessionManager(
            settings=settings, store=store, broker=broker, kinds=kinds, metrics=metrics
        )
        interrupted = await mgr.reconcile()
        if interrupted:
            log.warning("hermarchus.reconciled", interrupted=len(interrupted))
        state["manager"] = mgr
        installer.start()
        log.info("hermarchus.ready", profile=profile, kinds=sorted(kinds), version=__version__)
        try:
            yield
        finally:
            state.pop("manager", None)
            await installer.close()
            await mgr.shutdown()
            await store.close()

    app = FastAPI(title="Hermarchus", version=__version__, lifespan=lifespan)
    app.include_router(
        create_ops_router("hermarchus", version=__version__, registry=metrics.registry)
    )

    # ── access ────────────────────────────────────────────────────────────────
    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, value = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            value.strip().encode("utf-8"), token
        ):
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthenticated", "message": "a valid bearer token is required"},
                headers={"WWW-Authenticate": "Bearer"},
            )

    def require_profile(
        request: Request,
        tenant_id: Annotated[str | None, Query()] = None,
    ) -> None:
        named = [
            v
            for v in (
                request.headers.get("x-epicurus-profile"),
                request.headers.get("x-epicurus-tenant"),
                tenant_id,
            )
            if v is not None
        ]
        for value in named:
            try:
                validate_tenant_id(value)
            except TenantError as exc:
                raise _error(400, "invalid_profile", str(exc)) from exc
            if value != profile:
                raise _error(
                    403,
                    "wrong_profile",
                    f"this Hermarchus is bound to profile {profile!r}, not {value!r}",
                )

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_token), Depends(require_profile)])

    def _translate(exc: Exception) -> HTTPException:
        if isinstance(exc, NotFoundError):
            return _error(404, "not_found", str(exc))
        if isinstance(exc, ConflictError):
            return _error(409, exc.code, str(exc))
        if isinstance(exc, LimitError):
            return _error(429, "session_limit_reached", str(exc))
        if isinstance(exc, UnavailableError):
            return _error(409, "agent_unavailable", str(exc))
        if isinstance(exc, PathError):
            return _error(400, "invalid_cwd", str(exc))
        raise exc

    _known = (NotFoundError, ConflictError, LimitError, UnavailableError, PathError)

    @v1.get("/info")
    async def info() -> dict[str, Any]:
        mgr = manager()
        path = mgr.agent_path()
        return {
            "service": "hermarchus",
            "version": __version__,
            "contract_version": CONTRACT_VERSION,
            "acp_protocol_version": PROTOCOL_VERSION,
            "profile": profile,
            "limits": {
                "max_sessions": settings.max_sessions,
                "live_sessions": len(mgr.live),
                "stream_queue_size": settings.stream_queue_size,
                "permission_timeout_s": settings.permission_timeout_s,
                "unattended_timeout_s": settings.unattended_timeout_s,
                "unattended_default": settings.unattended,
            },
            "agents": [_kind_info(k, path, installer) for k in kinds.values()],
        }

    @v1.post("/sessions", status_code=201)
    async def create_session(body: CreateSession) -> dict[str, Any]:
        try:
            return await manager().create(
                agent=body.agent, cwd=body.cwd, title=body.title, unattended=body.unattended
            )
        except _known as exc:
            raise _translate(exc) from exc

    @v1.get("/sessions")
    async def list_sessions() -> dict[str, Any]:
        return {"sessions": await manager().list_all()}

    @v1.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        try:
            return await manager().get(session_id)
        except _known as exc:
            raise _translate(exc) from exc

    @v1.post("/sessions/{session_id}/input", status_code=202)
    async def send_input(session_id: str, body: SendInput) -> dict[str, Any]:
        try:
            return await manager().send_input(session_id, text=body.text, mode=body.mode)
        except _known as exc:
            raise _translate(exc) from exc

    @v1.post("/sessions/{session_id}/cancel", status_code=202)
    async def cancel(session_id: str) -> dict[str, Any]:
        try:
            await manager().cancel(session_id)
        except _known as exc:
            raise _translate(exc) from exc
        return {"status": "cancel_requested"}

    @v1.post("/sessions/{session_id}/stop")
    async def stop(session_id: str) -> dict[str, Any]:
        try:
            return await manager().stop(session_id)
        except _known as exc:
            raise _translate(exc) from exc

    @v1.delete("/sessions/{session_id}", status_code=204)
    async def delete(session_id: str) -> Response:
        try:
            await manager().delete(session_id)
        except _known as exc:
            raise _translate(exc) from exc
        return Response(status_code=204)

    @v1.get("/sessions/{session_id}/permissions")
    async def permissions(
        session_id: str, pending: Annotated[bool, Query()] = True
    ) -> dict[str, Any]:
        try:
            rows = await manager().permissions(session_id, pending_only=pending)
        except _known as exc:
            raise _translate(exc) from exc
        return {"permissions": rows}

    @v1.post("/sessions/{session_id}/permissions/{request_id}")
    async def answer(session_id: str, request_id: str, body: Answer) -> dict[str, Any]:
        try:
            event = await manager().answer(session_id, request_id, body.decision)
        except _known as exc:
            raise _translate(exc) from exc
        return {"request_id": request_id, "decision": body.decision, "seq": event.seq}

    @v1.get("/sessions/{session_id}/events")
    async def events(
        session_id: str,
        after: Annotated[int | None, Query(ge=0)] = None,
        follow: Annotated[bool, Query()] = False,
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        mgr = manager()
        try:
            await mgr.get(session_id)
        except _known as exc:
            raise _translate(exc) from exc
        start = after
        if start is None and last_event_id and last_event_id.strip().isdigit():
            start = int(last_event_id.strip())
        return StreamingResponse(
            stream_events(
                mgr,
                broker,
                session_id,
                after=start or 0,
                follow=follow,
                queue_size=settings.stream_queue_size,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app.include_router(v1)

    return app
