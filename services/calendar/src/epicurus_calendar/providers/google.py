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

import uuid
from datetime import UTC, datetime, timedelta

import httpx
from dateutil.rrule import rrule, rrulestr

from epicurus_calendar.models import Attendee, DateTimeRange, Event
from epicurus_calendar.providers.base import CalendarProvider, EditScope
from epicurus_calendar.recurrence import continue_from, truncate_before
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
        recurrence: str | None = None,
        attendees: list[Attendee] | None = None,
        recurrence_timezone: str | None = None,
        add_meet: bool = False,
    ) -> Event:
        # recurrence_timezone (#446) is unused here: Google expands recurrence server-side
        # and always returns each occurrence as a correct absolute instant, so it needs no
        # wall-clock anchor to counter DST drift the way the local provider does.
        del recurrence_timezone
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
        if recurrence:
            body["recurrence"] = [f"RRULE:{recurrence}"]
        if attendees:
            body["attendees"] = [{"email": a.email} for a in attendees]
        params: dict[str, int] = {}
        if add_meet:
            # conferenceDataVersion=1 (#444) is required for Google to act on
            # conferenceData at all; requestId is a client-chosen idempotency key for the
            # conference creation, not a lookup id we need to remember afterwards.
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": str(uuid.uuid4()),
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
            params["conferenceDataVersion"] = 1
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(
                f"{_CALENDAR_API}/calendars/{cal}/events",
                headers=headers,
                params=params,
                json=body,
            )
            resp.raise_for_status()
        return _google_item_to_event(resp.json())

    async def _resolve_series_id(self, *, tenant_id: str, event_id: str, calendar_id: str) -> str:
        """The series' own id for *event_id* — itself, or its series if it's an instance.

        ``edit_scope="all"`` must act on the series master, but the caller may have only
        an instance id (from a listing). Best-effort: a lookup failure falls back to
        *event_id* as given rather than blocking the edit (#432).
        """
        try:
            current = await self.get_event(
                tenant_id=tenant_id, event_id=event_id, calendar_id=calendar_id
            )
        except Exception:
            return event_id
        if current is not None and current.recurring_event_id:
            return current.recurring_event_id
        return event_id

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
        recurrence: str | None = None,
        attendees: list[Attendee] | None = None,
        recurrence_timezone: str | None = None,
        edit_scope: EditScope = "this",
    ) -> Event | None:
        """Patch an event via the Calendar API; ``None`` when Google reports it gone (404).

        Sends only the supplied fields (``events.patch`` is a partial update), so an
        edit that changes just the time leaves the title and description untouched. When
        *all_day* is given, the supplied ``start``/``end`` are written as ``date`` (all-day)
        or ``dateTime`` (timed) fields to match.

        ``edit_scope="this"`` (#432) patches *event_id* as given — Google natively turns
        patching an expanded instance id into a per-occurrence exception, no extra work
        needed. ``edit_scope="all"`` resolves to the series' own id first (in case
        *event_id* names one instance) and patches that, changing every occurrence.
        ``edit_scope="following"`` (#445) splits the series in two — see
        :meth:`_update_following`. *recurrence_timezone* (#446) is unused (see
        :meth:`create_event`).
        """
        del recurrence_timezone
        cal = calendar_id or self._calendar_id
        if edit_scope == "following":
            return await self._update_following(
                tenant_id=tenant_id,
                event_id=event_id,
                calendar_id=cal,
                title=title,
                start=start,
                end=end,
                description=description,
                location=location,
                all_day=all_day,
                recurrence=recurrence,
                attendees=attendees,
            )
        target_id = (
            await self._resolve_series_id(tenant_id=tenant_id, event_id=event_id, calendar_id=cal)
            if edit_scope == "all"
            else event_id
        )
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
        if recurrence == "":
            # Clear the series' rule (#532, ADR-0086): Google represents "no recurrence" as an
            # empty list, not a bare "RRULE:" (which 400s). Reached only via edit_scope='all'
            # (target resolved to the master) or 'following' (handled above) — the tool rejects
            # clearing a single occurrence, so a `recurrence: []` never lands on an instance id.
            body["recurrence"] = []
        elif recurrence:
            body["recurrence"] = [f"RRULE:{recurrence}"]
        if attendees is not None:
            body["attendees"] = [{"email": a.email} for a in attendees]
        if not body:
            # Nothing to change — return the event as-is rather than issue an empty patch.
            return await self.get_event(tenant_id=tenant_id, event_id=target_id, calendar_id=cal)
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.patch(
                f"{_CALENDAR_API}/calendars/{cal}/events/{target_id}",
                headers=headers,
                json=body,
            )
        if resp.status_code == httpx.codes.NOT_FOUND:
            return None
        resp.raise_for_status()
        return _google_item_to_event(resp.json())

    async def _update_following(
        self,
        *,
        tenant_id: str,
        event_id: str,
        calendar_id: str,
        title: str | None,
        start: datetime | None,
        end: datetime | None,
        description: str | None,
        location: str | None,
        all_day: bool | None,
        recurrence: str | None,
        attendees: list[Attendee] | None,
    ) -> Event | None:
        """Split the series *event_id* belongs to in two (#445, ``edit_scope="following"``).

        Google has no single-call support for this: PATCH the original master's RRULE to end
        just before *event_id*'s occurrence, then ``events.insert`` a new series starting
        there carrying the edited fields (defaulting to the occurrence's current ones).
        Best-effort — the two writes are not atomic (no cross-event transaction).
        """
        current = await self.get_event(
            tenant_id=tenant_id, event_id=event_id, calendar_id=calendar_id
        )
        if current is None:
            return None
        series_id = current.recurring_event_id
        if series_id is None:
            # event_id already names the series (or a one-off event) directly — nothing to
            # split; "following" degrades to editing it in place, same as "all".
            return await self.update_event(
                tenant_id=tenant_id,
                event_id=event_id,
                title=title,
                start=start,
                end=end,
                description=description,
                location=location,
                calendar_id=calendar_id,
                all_day=all_day,
                recurrence=recurrence,
                attendees=attendees,
                edit_scope="all",
            )
        master = await self.get_event(
            tenant_id=tenant_id, event_id=series_id, calendar_id=calendar_id
        )
        if master is None or master.recurrence is None:
            return None
        original_start = current.original_start or current.start
        parsed_rule = rrulestr(f"RRULE:{master.recurrence}", dtstart=master.start)
        if not isinstance(parsed_rule, rrule):
            return None  # a corrupt/legacy rule — degrade rather than crash
        truncated = truncate_before(master.recurrence, parsed_rule, original_start)
        if truncated is None:
            # No occurrence precedes the split point — splitting at the series' own first
            # occurrence is just editing the whole series.
            return await self.update_event(
                tenant_id=tenant_id,
                event_id=series_id,
                title=title,
                start=start,
                end=end,
                description=description,
                location=location,
                calendar_id=calendar_id,
                all_day=all_day,
                recurrence=recurrence,
                attendees=attendees,
                edit_scope="all",
            )
        new_recurrence = (
            recurrence
            if recurrence is not None
            else continue_from(master.recurrence, parsed_rule, original_start)
        )
        await self.update_event(
            tenant_id=tenant_id,
            event_id=series_id,
            recurrence=truncated,
            calendar_id=calendar_id,
            edit_scope="all",
        )
        return await self.create_event(
            tenant_id=tenant_id,
            title=title if title is not None else current.title,
            start=start if start is not None else current.start,
            end=end if end is not None else current.end,
            description=description if description is not None else current.description,
            location=location if location is not None else current.location,
            calendar_id=calendar_id,
            all_day=all_day if all_day is not None else current.all_day,
            recurrence=new_recurrence,
            attendees=attendees if attendees is not None else current.attendees,
        )

    async def delete_event(
        self,
        *,
        tenant_id: str,
        event_id: str,
        calendar_id: str | None = None,
        edit_scope: EditScope = "this",
    ) -> bool:
        """Delete an event via the Calendar API.

        Returns ``True`` on success; ``False`` when Google reports the event already
        gone (404/410), so the router can try the next enabled calendar (#208).
        ``edit_scope`` mirrors :meth:`update_event`: ``"this"`` (#432) deletes just the
        named occurrence (Google turns it into a cancelled exception); ``"following"``
        (#445) truncates the series so it ends just before that occurrence, removing it and
        every later one; ``"all"`` (#432) resolves to the series id first and deletes the
        whole series.
        """
        cal = calendar_id or self._calendar_id
        if edit_scope == "following":
            return await self._delete_following(
                tenant_id=tenant_id, event_id=event_id, calendar_id=cal
            )
        target_id = (
            await self._resolve_series_id(tenant_id=tenant_id, event_id=event_id, calendar_id=cal)
            if edit_scope == "all"
            else event_id
        )
        headers = await self._auth_headers()
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.delete(
                f"{_CALENDAR_API}/calendars/{cal}/events/{target_id}",
                headers=headers,
            )
        if resp.status_code in (httpx.codes.NOT_FOUND, httpx.codes.GONE):
            return False
        resp.raise_for_status()
        return True

    async def _delete_following(self, *, tenant_id: str, event_id: str, calendar_id: str) -> bool:
        """Truncate the series *event_id* belongs to so it ends just before its occurrence,
        removing it and every later occurrence (#445). Best-effort, mirroring
        :meth:`_update_following` — no cross-event transaction on Google's side.
        """
        current = await self.get_event(
            tenant_id=tenant_id, event_id=event_id, calendar_id=calendar_id
        )
        if current is None:
            return False
        series_id = current.recurring_event_id
        if series_id is None:
            return await self.delete_event(
                tenant_id=tenant_id, event_id=event_id, calendar_id=calendar_id, edit_scope="all"
            )
        master = await self.get_event(
            tenant_id=tenant_id, event_id=series_id, calendar_id=calendar_id
        )
        if master is None or master.recurrence is None:
            return False
        original_start = current.original_start or current.start
        parsed_rule = rrulestr(f"RRULE:{master.recurrence}", dtstart=master.start)
        if not isinstance(parsed_rule, rrule):
            return False
        truncated = truncate_before(master.recurrence, parsed_rule, original_start)
        if truncated is None:
            return await self.delete_event(
                tenant_id=tenant_id, event_id=series_id, calendar_id=calendar_id, edit_scope="all"
            )
        updated = await self.update_event(
            tenant_id=tenant_id,
            event_id=series_id,
            recurrence=truncated,
            calendar_id=calendar_id,
            edit_scope="all",
        )
        return updated is not None

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
        """Every calendar in the account's calendarList (ADR-0030), primary first.

        Each becomes a toggleable collection in the shell; ``writable`` reflects the
        Google access role so a read-only (subscribed) calendar can be kept out of the
        active/write picker. The account's **primary** calendar sorts first (the API's
        order is unspecified) so it is the natural default wherever "the first Google
        calendar" is picked — the New-event picker's preselection (#433).
        """
        headers = await self._auth_headers()
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.get(
                f"{_CALENDAR_API}/users/me/calendarList",
                headers=headers,
            )
            resp.raise_for_status()
        items = sorted(resp.json().get("items", []), key=lambda i: not i.get("primary", False))
        return [
            Collection(
                account="google",
                collection=str(item.get("id", "")),
                title=str(item.get("summaryOverride") or item.get("summary") or item.get("id", "")),
                writable=str(item.get("accessRole", "")) in _WRITABLE_ROLES,
                # The user's own calendar colour — the shell tints that calendar's
                # events and menu dot with it instead of a derived hue (#431).
                color=str(item["backgroundColor"]) if item.get("backgroundColor") else None,
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


def _google_recurrence_rule(item: dict[str, object]) -> str | None:
    """The bare RRULE (no ``"RRULE:"`` prefix) from a series master's ``recurrence`` list.

    Google's ``recurrence`` can carry ``RRULE``/``EXDATE``/``RDATE`` lines together; only
    the first ``RRULE:`` entry is mapped (#432) — a per-instance EXDATE Google already
    applies server-side to what ``events.list(singleEvents=true)`` returns, so it never
    needs to round-trip through our domain model.
    """
    raw = item.get("recurrence")
    if not isinstance(raw, list):
        return None
    rrule_lines = (line for line in raw if isinstance(line, str) and line.startswith("RRULE:"))
    return next((line[len("RRULE:") :] for line in rrule_lines), None)


def _google_original_start(item: dict[str, object]) -> datetime | None:
    """An exception instance's ``originalStartTime`` — the slot it overrides, if present."""
    raw = item.get("originalStartTime")
    if not isinstance(raw, dict):
        return None
    value = raw.get("dateTime") or raw.get("date")
    return datetime.fromisoformat(str(value)) if value else None


def _google_meet_url(item: dict[str, object]) -> str | None:
    """The Google Meet join link from ``conferenceData.entryPoints`` (#444), if any.

    A conference has one entry point per join method (video/phone/sip/more); the video
    one's ``uri`` is the ``meet.google.com/...`` link. ``None`` when the event carries no
    conference, or (rarely) the conference is still being provisioned and the response
    doesn't have entry points yet — best-effort, not retried.
    """
    conference = item.get("conferenceData")
    if not isinstance(conference, dict):
        return None
    entry_points = conference.get("entryPoints")
    if not isinstance(entry_points, list):
        return None
    for entry in entry_points:
        if isinstance(entry, dict) and entry.get("entryPointType") == "video":
            uri = entry.get("uri")
            return str(uri) if uri else None
    return None


def _google_attendees(item: dict[str, object]) -> list[Attendee]:
    raw = item.get("attendees")
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("email"):
            continue
        out.append(
            Attendee(
                email=str(entry["email"]),
                display_name=str(entry["displayName"]) if entry.get("displayName") else None,
                response_status=str(entry.get("responseStatus") or "needsAction"),
            )
        )
    return out


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
        recurrence=_google_recurrence_rule(item),
        recurring_event_id=str(item["recurringEventId"]) if item.get("recurringEventId") else None,
        original_start=_google_original_start(item),
        attendees=_google_attendees(item),
        meet_url=_google_meet_url(item),
    )
