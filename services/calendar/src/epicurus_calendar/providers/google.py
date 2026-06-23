"""Google Calendar provider — reads/creates events via the Google Calendar REST API.

The module never holds a client secret or refresh token.  It requests a valid
access token from the core platform API (``GET /platform/v1/oauth/google/token``)
on each operation; the core transparently refreshes expired tokens.

Requires the tenant to have connected their Google account via the OAuth flow
with the ``https://www.googleapis.com/auth/calendar`` scope.  The connect flow
is initiated from the web UI (Settings → Connect Google); the core's OAuth
service manages the token lifecycle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from epicurus_calendar.models import DateTimeRange, Event
from epicurus_calendar.providers.base import CalendarProvider
from epicurus_core import Collection, PlatformClient

_CALENDAR_API = "https://www.googleapis.com/calendar/v3"

# Google calendarList access roles that permit creating/editing events.
_WRITABLE_ROLES = {"writer", "owner"}


class GoogleCalendarProvider(CalendarProvider):
    """Google Calendar-backed provider.

    Args:
        platform: A ``PlatformClient`` scoped to this service's tenant; used
            to fetch OAuth tokens from the core vault.
        calendar_id: The Google Calendar ID to read/write.  ``"primary"``
            resolves to the authenticated user's default calendar.
    """

    name = "google"

    def __init__(self, platform: PlatformClient, calendar_id: str = "primary") -> None:
        self._platform = platform
        # The fallback calendar when a call passes no explicit ``calendar_id`` (e.g. the
        # local-only unit tests). With the account/collection model the router supplies the
        # operator-selected calendar per call (ADR-0030).
        self._calendar_id = calendar_id

    async def _auth_headers(self) -> dict[str, str]:
        token = await self._platform.get_oauth_token("google")
        return {"Authorization": f"Bearer {token}"}

    async def get_timezone(self, *, tenant_id: str) -> str | None:
        """The user's Google Calendar timezone (IANA), or ``None`` best-effort (ADR-0039).

        Reads ``GET /users/me/settings/timezone``; any failure (not connected, API error)
        returns ``None`` so callers degrade rather than break.
        """
        try:
            headers = await self._auth_headers()
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.get(
                    f"{_CALENDAR_API}/users/me/settings/timezone", headers=headers
                )
                resp.raise_for_status()
            value = resp.json().get("value")
            return value if isinstance(value, str) else None
        except Exception:
            return None

    async def list_events(
        self,
        *,
        tenant_id: str,
        time_range: DateTimeRange,
        calendar_id: str | None = None,
    ) -> list[Event]:
        cal = calendar_id or self._calendar_id
        headers = await self._auth_headers()
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.get(
                f"{_CALENDAR_API}/calendars/{cal}/events",
                headers=headers,
                params={
                    "timeMin": _to_rfc3339(time_range.start),
                    "timeMax": _to_rfc3339(time_range.end),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                },
            )
            resp.raise_for_status()
        return [_google_item_to_event(item) for item in resp.json().get("items", [])]

    async def get_event(
        self, *, tenant_id: str, event_id: str, calendar_id: str | None = None
    ) -> Event | None:
        """Fetch a single event by id; ``None`` when Google reports it gone (404)."""
        cal = calendar_id or self._calendar_id
        headers = await self._auth_headers()
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.get(
                f"{_CALENDAR_API}/calendars/{cal}/events/{event_id}",
                headers=headers,
            )
        if resp.status_code == httpx.codes.NOT_FOUND:
            return None
        resp.raise_for_status()
        return _google_item_to_event(resp.json())

    async def create_event(
        self,
        *,
        tenant_id: str,
        title: str,
        start: datetime,
        end: datetime,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str | None = None,
        all_day: bool = False,
    ) -> Event:
        cal = calendar_id or self._calendar_id
        headers = await self._auth_headers()
        body: dict[str, object] = {
            "summary": title,
            "start": _google_when(start, all_day=all_day),
            "end": _google_when(end, all_day=all_day),
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(
                f"{_CALENDAR_API}/calendars/{cal}/events",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
        return _google_item_to_event(resp.json())

    async def update_event(
        self,
        *,
        tenant_id: str,
        event_id: str,
        title: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str | None = None,
        all_day: bool | None = None,
    ) -> Event | None:
        """Patch an event via the Calendar API; ``None`` when Google reports it gone (404).

        Sends only the supplied fields (``events.patch`` is a partial update), so an
        edit that changes just the time leaves the title and description untouched. When
        *all_day* is given, the supplied ``start``/``end`` are written as ``date`` (all-day)
        or ``dateTime`` (timed) fields to match.
        """
        cal = calendar_id or self._calendar_id
        headers = await self._auth_headers()
        # An edit that only flips all-day still has to resend both endpoints, since
        # Google rejects a ``start`` with ``date`` against an existing ``end`` with
        # ``dateTime`` (and vice-versa); treat the flag as also touching start/end.
        whole_day = bool(all_day)
        body: dict[str, object] = {}
        if title is not None:
            body["summary"] = title
        if start is not None:
            body["start"] = _google_when(start, all_day=whole_day)
        if end is not None:
            body["end"] = _google_when(end, all_day=whole_day)
        if description is not None:
            body["description"] = description
        if location is not None:
            body["location"] = location
        if not body:
            # Nothing to change — return the event as-is rather than issue an empty patch.
            return await self.get_event(tenant_id=tenant_id, event_id=event_id, calendar_id=cal)
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.patch(
                f"{_CALENDAR_API}/calendars/{cal}/events/{event_id}",
                headers=headers,
                json=body,
            )
        if resp.status_code == httpx.codes.NOT_FOUND:
            return None
        resp.raise_for_status()
        return _google_item_to_event(resp.json())

    async def delete_event(
        self, *, tenant_id: str, event_id: str, calendar_id: str | None = None
    ) -> bool:
        """Delete an event via the Calendar API.

        Returns ``True`` on success; ``False`` when Google reports the event already
        gone (404/410), so the router can try the next enabled calendar (#208).
        """
        cal = calendar_id or self._calendar_id
        headers = await self._auth_headers()
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.delete(
                f"{_CALENDAR_API}/calendars/{cal}/events/{event_id}",
                headers=headers,
            )
        if resp.status_code in (httpx.codes.NOT_FOUND, httpx.codes.GONE):
            return False
        resp.raise_for_status()
        return True

    async def find_free_slots(
        self,
        *,
        tenant_id: str,
        time_range: DateTimeRange,
        duration_minutes: int,
        calendar_id: str | None = None,
    ) -> list[DateTimeRange]:
        """Query the Google Free/Busy API and return open slots."""
        cal = calendar_id or self._calendar_id
        headers = await self._auth_headers()
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(
                f"{_CALENDAR_API}/freeBusy",
                headers=headers,
                json={
                    "timeMin": _to_rfc3339(time_range.start),
                    "timeMax": _to_rfc3339(time_range.end),
                    "items": [{"id": cal}],
                },
            )
            resp.raise_for_status()
        busy_raw = resp.json().get("calendars", {}).get(cal, {}).get("busy", [])
        busy = [
            (
                _parse_rfc3339(b["start"]),
                _parse_rfc3339(b["end"]),
            )
            for b in busy_raw
        ]
        from epicurus_calendar.providers.local import _compute_free_slots

        return _compute_free_slots(
            busy=busy,
            window_start=time_range.start,
            window_end=time_range.end,
            min_duration=timedelta(minutes=duration_minutes),
        )

    async def is_available(self, *, tenant_id: str) -> bool:
        """True when a Google token is stored for this tenant.

        Any HTTP failure — not connected (4xx) or the core being unreachable — means
        "not available" rather than an error, so a status check never raises.
        """
        try:
            await self._platform.get_oauth_token("google")
            return True
        except httpx.HTTPError:
            return False

    async def list_collections(self, *, tenant_id: str) -> list[Collection]:
        """Every calendar in the account's calendarList (ADR-0030).

        Each becomes a toggleable collection in the shell; ``writable`` reflects the
        Google access role so a read-only (subscribed) calendar can be kept out of the
        active/write picker.
        """
        headers = await self._auth_headers()
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.get(
                f"{_CALENDAR_API}/users/me/calendarList",
                headers=headers,
            )
            resp.raise_for_status()
        items = resp.json().get("items", [])
        return [
            Collection(
                account="google",
                collection=str(item.get("id", "")),
                title=str(item.get("summaryOverride") or item.get("summary") or item.get("id", "")),
                writable=str(item.get("accessRole", "")) in _WRITABLE_ROLES,
            )
            for item in items
        ]


def _to_rfc3339(dt: datetime) -> str:
    """Format *dt* as an RFC-3339 string with a UTC offset, as Google requires."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _google_when(dt: datetime, *, all_day: bool) -> dict[str, str]:
    """The Google start/end object for *dt*: a ``date`` for all-day, else a ``dateTime``.

    All-day events carry the floating calendar date (Google's ``date`` field, UTC date
    of the midnight boundary), never a timed instant — sending a ``dateTime`` would make
    the event land at a wall-clock time and shift across days for non-UTC viewers.
    """
    if all_day:
        return {"date": dt.astimezone(UTC).date().isoformat()}
    return {"dateTime": _to_rfc3339(dt)}


def _parse_rfc3339(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _google_item_to_event(item: dict[str, object]) -> Event:
    """Map one Google Calendar event item to the domain ``Event`` model.

    A Google all-day event carries ``start.date``/``end.date`` (no time) instead of
    ``dateTime``. Those are parsed to UTC-midnight boundaries and flagged ``all_day`` so
    the shell renders them on their calendar date with no timezone conversion — fixing the
    "one day early" off-by-one that treating a date as a UTC instant caused.
    """
    start_raw = item.get("start", {})
    end_raw = item.get("end", {})
    assert isinstance(start_raw, dict)
    assert isinstance(end_raw, dict)
    all_day = "date" in start_raw and "dateTime" not in start_raw
    start_str = str(start_raw.get("dateTime") or start_raw.get("date", ""))
    end_str = str(end_raw.get("dateTime") or end_raw.get("date", ""))
    return Event(
        id=str(item.get("id", "")),
        title=str(item.get("summary", "(no title)")),
        start=datetime.fromisoformat(start_str),
        end=datetime.fromisoformat(end_str),
        description=str(item["description"]) if item.get("description") else None,
        location=str(item["location"]) if item.get("location") else None,
        provider="google",
        all_day=all_day,
    )
