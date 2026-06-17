"""GoogleTasksProvider — lists and manages tasks via the Google Tasks API.

OAuth tokens are fetched from the core via ``PlatformClient.get_oauth_token``
(which calls ``GET /platform/v1/oauth/google/token``) — no client secret or
refresh token ever leaves the core (ADR-0020 / non-negotiable #8).
"""

from __future__ import annotations

from typing import Any

import httpx

from epicurus_core import PlatformClient
from epicurus_tasks.models import Task

_TASKS_BASE = "https://tasks.googleapis.com/tasks/v1"
_DEFAULT_LIST = "@default"


class GoogleTasksError(RuntimeError):
    """Raised when a Google Tasks API call fails."""


class GoogleTasksProvider:
    """Manages tasks via the Google Tasks REST API.

    Args:
        platform: A ``PlatformClient`` scoped to this service's tenant; used to
            fetch the Google OAuth token from the core (it never holds the token).
    """

    def __init__(self, platform: PlatformClient) -> None:
        self._platform = platform

    def provider_name(self) -> str:
        return "google"

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

        completed_raw: str | None = item.get("completed")
        return Task(
            id=item["id"],
            title=item.get("title", ""),
            notes=item.get("notes") or None,
            due=due,
            completed=item.get("status") == "completed",
            completed_at=completed_raw,
        )

    async def list_tasks(self, tenant_id: str, *, list_id: str | None = None) -> list[Task]:
        """Return incomplete tasks from the specified (or default) Google task list."""
        tasklist = list_id or _DEFAULT_LIST
        token = await self._access_token()
        async with httpx.AsyncClient(base_url=_TASKS_BASE, timeout=15.0) as client:
            resp = await client.get(
                f"/lists/{tasklist}/tasks",
                headers=self._auth_headers(token),
                params={"showCompleted": "false", "showHidden": "false"},
            )
            if resp.status_code == 401:
                raise GoogleTasksError(
                    "Google token is invalid or revoked — reconnect via Settings"
                )
            resp.raise_for_status()
            items: list[dict[str, Any]] = resp.json().get("items", [])
        return [self._parse_task(item) for item in items]

    async def add_task(
        self,
        tenant_id: str,
        title: str,
        *,
        notes: str | None = None,
        due: str | None = None,
        list_id: str | None = None,
    ) -> Task:
        """Create a task in the specified (or default) Google task list."""
        tasklist = list_id or _DEFAULT_LIST
        token = await self._access_token()
        body: dict[str, Any] = {"title": title}
        if notes:
            body["notes"] = notes
        if due:
            # Google Tasks expects RFC 3339 UTC midnight for due dates.
            body["due"] = f"{due[:10]}T00:00:00.000Z"
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
            return self._parse_task(resp.json())

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
            return self._parse_task(resp.json())

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
                return None
            if resp.status_code == 401:
                raise GoogleTasksError(
                    "Google token is invalid or revoked — reconnect via Settings"
                )
            resp.raise_for_status()
            return self._parse_task(resp.json())

    async def update_task(
        self,
        tenant_id: str,
        task_id: str,
        *,
        title: str | None = None,
        notes: str | None = None,
        due: str | None = None,
        list_id: str | None = None,
    ) -> Task:
        """Edit a task's title/notes/due via a PATCH to the Google Tasks API.

        Only the supplied fields are sent. With nothing to change it GETs and
        returns the current task, so the call is always a clean read-or-edit.
        """
        tasklist = list_id or _DEFAULT_LIST
        token = await self._access_token()
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if notes is not None:
            body["notes"] = notes
        if due:
            # Google Tasks expects RFC 3339 UTC midnight for due dates.
            body["due"] = f"{due[:10]}T00:00:00.000Z"

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
            return self._parse_task(resp.json())
