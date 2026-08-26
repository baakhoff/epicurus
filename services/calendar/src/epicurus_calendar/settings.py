"""Calendar-service configuration — CoreSettings plus calendar-specific fields."""

from __future__ import annotations

from epicurus_core import CoreSettings


class CalendarSettings(CoreSettings):
    """Adds storage endpoints to shared settings.

    There is no provider selection any more (ADR-0030): the module always backs itself
    with the local store and routes to connected Google calendars per the operator's
    selection, which lives in the core (``module_prefs``), not in service config.
    """

    # Async Postgres DSN for the local default store.
    database_url: str = "postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus"

    # Core service base URL (platform API). On the Docker network: http://core-app:8080.
    platform_url: str = "http://localhost:8080"

    # How often the lead-time scheduler ticks (#664) — event_starting_soon/event_ended. 60s
    # keeps a 15-minute default lead accurate to within about a minute without hammering the
    # provider (Google Calendar) every few seconds.
    scheduler_poll_interval_s: float = 60.0

    # How often the reconcile loop pulls each watched calendar's delta (#831) — the loop that
    # makes event_created/event_updated/event_cancelled fire for a change made in Google
    # Calendar's own UI. **On by default**: an event contract covering only changes made
    # through this module is not a contract, and every downstream consumer (automations, push
    # alerts) is already wired for it. A tick with nothing connected or enabled costs nothing —
    # no target resolves, so no provider call is made. Matched to mail's poller rather than the
    # lead-time scheduler's 60s: an external edit is not minute-critical. ``0`` disables it.
    sync_poll_interval_s: float = 300.0
    # Fraction of the interval each sleep is randomised by (±). Calendar runs two periodic
    # loops in one process; a little jitter keeps them from settling into lockstep. 0 disables.
    sync_poll_jitter_frac: float = 0.1
    # How far back a first sync anchors its window. Google binds ``timeMin`` to the sync token
    # it mints, so this is also the window every later incremental call inherits.
    sync_window_days: int = 30
    # How long a self-write marker stays valid — the window in which the reconcile still
    # recognises a change this module made itself. Comfortably longer than the poll interval so
    # a write is always covered by the tick that observes it, short enough that a lingering
    # series marker cannot mask a genuinely external edit for long.
    sync_self_write_ttl_s: float = 900.0
    # Ceiling on how many events one collection's reconcile pass may announce. A bound on the
    # notification burst, never on the sync itself: the cache is updated in full regardless.
    sync_max_emissions_per_pass: int = 50
