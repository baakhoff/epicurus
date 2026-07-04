"""GoogleTasksProvider — lists and manages tasks via the Google Tasks API.

OAuth tokens are fetched from the core via ``PlatformClient.get_oauth_token``
(which calls ``GET /platform/v1/oauth/google/token``) — no client secret or
refresh token ever leaves the core (ADR-0020 / non-negotiable #8).

Field mapping / provider limits (documented per ADR-0016 / issue #218):
- title, notes, due → mapped bidirectionally.
- status "done" ↔ Google "completed"; "open"/"in_progress" ↔ Google "needsAction".
  On read-back "in_progress" degrades to "open" because Google has no such state.
- priority, tags → local-only; silently ignored when writing; always None/[] on read.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

from epicurus_core import Collection, PlatformClient
from epicurus_tasks.db import RepeatStore
from epicurus_tasks.models import Task, TaskScope

_TASKS_BASE = "https://tasks.googleapis.com/tasks/v1"
_DEFAULT_LIST = "@default"


class GoogleTasksError(RuntimeError):
    """Raised when a Google Tasks API call fails."""


class GoogleTasksProvider:
    """Manages tasks via the Google Tasks REST API.

    Google Tasks has **no recurrence field** (repeat is UI-only), so a repeating task's rule
    is emulated module-side (ADR-0082): stored in a tenant-scoped :class:`RepeatStore` side
    table keyed by the provider list + task id, filled onto reads and retired (GC) when the
    task is deleted or a lookup misses. Without a store (unit tests) recurrence silently
    degrades — ``repeat`` reads back ``None`` and writes are no-ops.

    Args:
        platform: A ``PlatformClient`` scoped to this service's tenant; used to
            fetch the Google OAuth token from the core (it never holds the token).
        repeats: Side-table store for emulated recurrence rules; ``None`` disables
            recurrence support (e.g. in unit tests without a database).
    """

    def __init__(self, platform: PlatformClient, *, repeats: RepeatStore | None = None) -> None:
        self._platform = platform
        self._repeats = repeats

    def _list_key(self, list_id: str | None) -> str:
        """The side-table list key — the same string this provider resolves for the API."""
        return list_id or _DEFAULT_LIST

    async def _fill_repeat(self, tenant_id: str, list_id: str | None, task: Task) -> Task:
        """Attach the task's emulated recurrence rule from the side table, if any."""
        if self._repeats is None:
            return task
        rule = await self._repeats.get(
            tenant_id=tenant_id, list_id=self._list_key(list_id), task_id=task.id
        )
        return task.model_copy(update={"repeat": rule}) if rule else task

    def provider_name(self) -> str:
        return "google"

    async def is_available(self, tenant_id: str) -> bool:
        """True when a Google token is stored for this tenant (ADR-0030).

        Any HTTP failure — not connected (4xx) or the core being unreachable — means
        "not available" rather than an error, so a status check never raises.
        """
        try:
            await self._platform.get_oauth_token("google")
            return True
        except httpx.HTTPError:
            return False

    async def list_collections(self, tenant_id: str) -> list[Collection]:
        """Every Google task list in the account (ADR-0030).

        Each becomes a switchable collection in the shell; task lists are always
        writable, so ``writable`` is True.
        """
        token = await self._access_token()
        async with httpx.AsyncClient(base_url=_TASKS_BASE, timeout=15.0) as client:
            resp = await client.get(
                "/users/@me/lists",
                headers=self._auth_headers(token),
            )
            if resp.status_code == 401:
                raise GoogleTasksError(
                    "Google token is invalid or revoked — reconnect via Settings"
                )
            resp.raise_for_status()
            items: list[dict[str, Any]] = resp.json().get("items", [])
        return [
            Collection(
                account="google",
                collection=str(item.get("id", "")),
                title=str(item.get("title") or item.get("id", "")),
                writable=True,
            )
            for item in items
        ]

    async def create_list(self, tenant_id: str, title: str) -> Collection:
        """Create a new Google task list (``tasklists.insert``) and return it (#474)."""
        token = await self._access_token()
        async with httpx.AsyncClient(base_url=_TASKS_BASE, timeout=15.0) as client:
            resp = await client.post(
                "/users/@me/lists",
                headers=self._auth_headers(token),
                json={"title": title},
            )
            if resp.status_code == 401:
                raise GoogleTasksError(
                    "Google token is invalid or revoked — reconnect via Settings"
                )
            resp.raise_for_status()
            item: dict[str, Any] = resp.json()
        return Collection(
            account="google",
            collection=str(item.get("id", "")),
            title=str(item.get("title") or title),
            writable=True,
        )

    async def _access_token(self) -> str:
        """Fetch a valid Google access token from the core via PlatformClient."""
        try:
            return await self._platform.get_oauth_token("google")
        except httpx.HTTPStatusError as exc:
            raise GoogleTasksError(
                "Google account not connected — connect via the Settings screen"
            ) from exc

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _parse_task(item: dict[str, Any]) -> Task:
        due_raw: str | None = item.get("due")
        # Google returns full RFC3339 timestamps for due dates; strip to date-only.
        due: str | None = None
        if due_raw:
            due = due_raw[:10]  # "2025-01-15T00:00:00.000Z" → "2025-01-15"

        google_status = item.get("status", "needsAction")
        # Google only has needsAction / completed — "in_progress" isn't stored there.
        status: Literal["open", "done"] = "done" if google_status == "completed" else "open"

        completed_raw: str | None = item.get("completed")
        return Task(
            id=item["id"],
            title=item.get("title", ""),
            notes=item.get("notes") or None,
            due=due,
            status=status,
            completed_at=completed_raw,
            # priority and tags are local-only; Google has no equivalent fields.
        )

    async def list_tasks(
        self, tenant_id: str, *, list_id: str | None = None, scope: TaskScope = "open"
    ) -> list[Task]:
        """Return tasks from the specified (or default) Google task list, filtered by *scope*.

        ``"open"`` (default) asks Google for incomplete tasks only; ``"done"`` / ``"all"``
        request completed (and hidden) tasks too — Google only surfaces completed tasks when
        ``showCompleted`` *and* ``showHidden`` are set, since it auto-hides them. For
        ``"done"`` the completed subset is kept client-side (ADR-0049).
        """
        tasklist = list_id or _DEFAULT_LIST
        token = await self._access_token()
        params = (
            {"showCompleted": "false", "showHidden": "false"}
            if scope == "open"
            else {"showCompleted": "true", "showHidden": "true"}
        )
        async with httpx.AsyncClient(base_url=_TASKS_BASE, timeout=15.0) as client:
            resp = await client.get(
                f"/lists/{tasklist}/tasks",
                headers=self._auth_headers(token),
                params=params,
            )
            if resp.status_code == 401:
                raise GoogleTasksError(
                    "Google token is invalid or revoked — reconnect via Settings"
                )
            resp.raise_for_status()
            items: list[dict[str, Any]] = resp.json().get("items", [])
        tasks = [self._parse_task(item) for item in items]
        if scope == "done":
            tasks = [t for t in tasks if t.status == "done"]
        # Attach emulated recurrence rules (ADR-0082) in one query, not one per task.
        if self._repeats is not None and tasks:
            rules = await self._repeats.get_many(
                tenant_id=tenant_id,
                list_id=self._list_key(list_id),
                task_ids=[t.id for t in tasks],
            )
            tasks = [
                t.model_copy(update={"repeat": rules[t.id]}) if t.id in rules else t for t in tasks
            ]
        return tasks

    async def add_task(
        self,
        tenant_id: str,
        title: str,
        *,
        notes: str | None = None,
        due: str | None = None,
        status: str = "open",
        priority: str | None = None,
        tags: list[str] | None = None,
        list_id: str | None = None,
        repeat: str | None = None,
    ) -> Task:
        """Create a task in the specified (or default) Google task list.

        ``priority`` and ``tags`` are silently ignored — Google Tasks has no
        equivalent fields. ``"in_progress"`` status is sent as ``"needsAction"``.
        ``repeat`` (an RRULE) is stored in the side table keyed by the new task id
        (Google has no recurrence field, ADR-0082).
        """
        tasklist = list_id or _DEFAULT_LIST
        token = await self._access_token()
        body: dict[str, Any] = {"title": title}
        if notes:
            body["notes"] = notes
        if due:
            # Google Tasks expects RFC 3339 UTC midnight for due dates.
            body["due"] = f"{due[:10]}T00:00:00.000Z"
        if status == "done":
            body["status"] = "completed"
        async with httpx.AsyncClient(base_url=_TASKS_BASE, timeout=15.0) as client:
            resp = await client.post(
                f"/lists/{tasklist}/tasks",
                headers=self._auth_headers(token),
                json=body,
            )
            if resp.status_code == 401:
                raise GoogleTasksError(
                    "Google token is invalid or revoked — reconnect via Settings"
                )
            resp.raise_for_status()
            task = self._parse_task(resp.json())
        if self._repeats is not None and repeat:
            await self._repeats.set(
                tenant_id=tenant_id, list_id=tasklist, task_id=task.id, rrule=repeat
            )
            task = task.model_copy(update={"repeat": repeat})
        return task

    async def complete_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> Task:
        """Mark a task complete using a PATCH to the Google Tasks API."""
        tasklist = list_id or _DEFAULT_LIST
        token = await self._access_token()
        async with httpx.AsyncClient(base_url=_TASKS_BASE, timeout=15.0) as client:
            resp = await client.patch(
                f"/lists/{tasklist}/tasks/{task_id}",
                headers=self._auth_headers(token),
                json={"status": "completed"},
            )
            if resp.status_code == 401:
                raise GoogleTasksError(
                    "Google token is invalid or revoked — reconnect via Settings"
                )
            if resp.status_code == 404:
                raise GoogleTasksError(f"task {task_id!r} not found in list {tasklist!r}")
            resp.raise_for_status()
            task = self._parse_task(resp.json())
        # A completed recurring task must carry its rule so the router can materialize the
        # next instance (ADR-0082) — Google's response never includes it.
        return await self._fill_repeat(tenant_id, list_id, task)

    async def delete_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> None:
        """Delete a task via the Google Tasks API.

        Backs moving a task between lists (recreate in target + delete here — Google has
        no cross-list move). A 404 is treated as already-gone, so a move whose source was
        removed concurrently still succeeds; a 401 surfaces a reconnect hint. Any emulated
        recurrence rule for the task is retired (ADR-0082 GC).
        """
        tasklist = list_id or _DEFAULT_LIST
        token = await self._access_token()
        async with httpx.AsyncClient(base_url=_TASKS_BASE, timeout=15.0) as client:
            resp = await client.delete(
                f"/lists/{tasklist}/tasks/{task_id}",
                headers=self._auth_headers(token),
            )
            if resp.status_code == 401:
                raise GoogleTasksError(
                    "Google token is invalid or revoked — reconnect via Settings"
                )
            if resp.status_code != 404:  # 404 = already gone; still retire any rule below
                resp.raise_for_status()
        if self._repeats is not None:
            await self._repeats.delete(tenant_id=tenant_id, list_id=tasklist, task_id=task_id)

    async def get_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> Task | None:
        """Fetch a single task from the specified (or default) Google task list.

        Returns ``None`` when the task does not exist (HTTP 404) so the caller can
        surface a clean 404; an auth failure still raises :class:`GoogleTasksError`.
        """
        tasklist = list_id or _DEFAULT_LIST
        token = await self._access_token()
        async with httpx.AsyncClient(base_url=_TASKS_BASE, timeout=15.0) as client:
            resp = await client.get(
                f"/lists/{tasklist}/tasks/{task_id}",
                headers=self._auth_headers(token),
            )
            if resp.status_code == 404:
                # GC on miss (ADR-0082): a task deleted in Google's own UI retires its rule
                # the next time we look it up, so an orphaned rule never lingers or errors.
                if self._repeats is not None:
                    await self._repeats.delete(
                        tenant_id=tenant_id, list_id=tasklist, task_id=task_id
                    )
                return None
            if resp.status_code == 401:
                raise GoogleTasksError(
                    "Google token is invalid or revoked — reconnect via Settings"
                )
            resp.raise_for_status()
            task = self._parse_task(resp.json())
        return await self._fill_repeat(tenant_id, list_id, task)

    async def update_task(
        self,
        tenant_id: str,
        task_id: str,
        *,
        title: str | None = None,
        notes: str | None = None,
        due: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        tags: list[str] | None = None,
        list_id: str | None = None,
        to_list_id: str | None = None,  # ignored here; cross-list moves go through the router
        repeat: str | None = None,
    ) -> Task:
        """Edit a task's title/notes/due/status via a PATCH to the Google Tasks API.

        An empty string clears ``due`` or ``notes``; ``None`` leaves them unchanged
        (#475). ``priority`` and ``tags`` are silently ignored. ``"in_progress"``
        status is sent as ``"needsAction"`` and will read back as ``"open"``. With
        nothing Google-mappable to change, GETs and returns the current task.
        ``to_list_id`` is ignored — the router performs cross-list moves
        (recreate+delete, ADR-0038). ``repeat`` (an RRULE, ``""`` clears it) is *not*
        Google-mappable — it is written to the side table, never the API body (ADR-0082).
        """
        tasklist = list_id or _DEFAULT_LIST
        token = await self._access_token()
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if notes is not None:
            body["notes"] = notes
        if due == "":
            body["due"] = None  # explicit clear — Google Tasks accepts a null due (#475)
        elif due:
            # Google Tasks expects RFC 3339 UTC midnight for due dates.
            body["due"] = f"{due[:10]}T00:00:00.000Z"
        if status is not None:
            body["status"] = "completed" if status == "done" else "needsAction"

        async with httpx.AsyncClient(base_url=_TASKS_BASE, timeout=15.0) as client:
            if body:
                resp = await client.patch(
                    f"/lists/{tasklist}/tasks/{task_id}",
                    headers=self._auth_headers(token),
                    json=body,
                )
            else:
                resp = await client.get(
                    f"/lists/{tasklist}/tasks/{task_id}",
                    headers=self._auth_headers(token),
                )
            if resp.status_code == 401:
                raise GoogleTasksError(
                    "Google token is invalid or revoked — reconnect via Settings"
                )
            if resp.status_code == 404:
                raise GoogleTasksError(f"task {task_id!r} not found in list {tasklist!r}")
            resp.raise_for_status()
            task = self._parse_task(resp.json())
        # Recurrence lives in the side table, not Google (ADR-0082): persist a change here,
        # then re-attach the stored rule so the returned task reflects it.
        if repeat is not None and self._repeats is not None:
            await self._repeats.set(
                tenant_id=tenant_id, list_id=tasklist, task_id=task_id, rrule=repeat or None
            )
        return await self._fill_repeat(tenant_id, list_id, task)
