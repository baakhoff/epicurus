"""Shared scheduling helpers — sleep until a local hour in the operator's timezone.

The nightly extraction drain (ADR-0051) and the maintenance orchestrator (ADR-0060) both wake at a
configured local hour. This is the one place that logic lives so the two schedulers stay consistent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

# A provider of the operator's IANA timezone string (e.g. "Europe/Belgrade"); blank/unknown → UTC.
TimezoneProvider = Callable[[], Awaitable[str]]


def seconds_until_hour(now: datetime, hour: int) -> float:
    """Seconds from *now* until the next occurrence of local *hour*:00 (today or tomorrow)."""
    target = now.replace(hour=hour % 24, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def sleep_until_hour(hour: int, timezone: TimezoneProvider) -> None:
    """Sleep until the next *hour*:00 in the operator's timezone (UTC if blank/unknown/bad)."""
    tz: tzinfo
    try:
        tz = ZoneInfo((await timezone()).strip() or "UTC")
    except Exception:  # unknown / blank / bad tz — fall back to UTC rather than skip a run
        tz = UTC
    await asyncio.sleep(seconds_until_hour(datetime.now(tz), hour))
