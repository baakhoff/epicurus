"""Folding scheduled turns into automations (ADR-0105, owner-decided).

#614's scheduled turns were the time-driven half of proactivity, built before the
event-driven half existed. An automation with a schedule trigger and a rolling chat sink
*is* a scheduled turn — same cadence vocabulary, same headless turn, same delivery into a
session — so keeping both would mean two schedulers, two tables, two pages, and two places
to look when something did or didn't run.

This migrates the rows. It runs at startup, is idempotent, and is deliberately
**non-destructive**: the ``scheduled_turns`` table is left intact and its rows are marked
as migrated rather than deleted. A migration that drops data on first boot has no way back
if it turns out to be wrong, and the cost of leaving a small, unread table behind is
nothing.

Each migrated turn keeps:

* its **cadence, hour, and weekday** — the vocabularies are identical by design;
* its **delivery target** as the rolling chat session id, so the operator's existing
  history stays exactly where they left it rather than restarting in a new thread;
* its **enabled** flag and its ``last_run_at`` — so a turn that already ran today does not
  run again the moment it becomes an automation.

It gets ``autonomy="notify"``: a scheduled turn could only ever summarize (the headless
path structurally cannot send — see ADR-0105), so ``notify`` is what it *already was*.
Promoting it silently to something that can act would be a migration changing behaviour,
which is the one thing a migration must not do.
"""

from __future__ import annotations

from epicurus_core import get_logger
from epicurus_core_app.automations.model import ScheduleTrigger
from epicurus_core_app.automations.store import AutomationStore
from epicurus_core_app.scheduled_turns import ScheduledTurnStore

log = get_logger("epicurus_core_app.automations.migration")

#: Marks a migrated scheduled turn, so a second run is a no-op. Written to the turn's
#: ``last_status`` — the one free-text column the row already has, which avoids adding a
#: column to a table being retired.
MIGRATED_MARKER = "migrated to automation"

#: The source recorded on the resulting automation, so the Automations page can explain
#: where a row the operator never created came from.
MIGRATED_SOURCE = "user"


async def migrate_scheduled_turns(turns: ScheduledTurnStore, automations: AutomationStore) -> int:
    """Migrate every un-migrated scheduled turn into an automation. Returns how many moved.

    Idempotent: a turn already marked as migrated is skipped, so this is safe to call on
    every startup. Best-effort per row — one bad turn is logged and skipped rather than
    aborting the rest, since a half-migrated set is still better than none and the marker
    makes the next boot retry only what failed.
    """
    moved = 0
    for turn in await turns.list_all():
        if turn.last_status == MIGRATED_MARKER:
            continue
        try:
            automation = await automations.create(
                tenant=turn.tenant,
                name=_name_for(turn.prompt),
                prompt=turn.prompt,
                # What it already was: a scheduled turn could only summarize.
                autonomy="notify",
                source=MIGRATED_SOURCE,
                schedule_trigger=ScheduleTrigger(
                    cadence=turn.cadence, hour=turn.hour, weekday=turn.weekday
                ),
                sinks=["chat"],
                chat_mode="rolling",
                # Its existing session, so the operator's history stays put.
                chat_session_id=turn.delivery_target,
                enabled=turn.enabled,
            )
            if turn.last_run_at is not None:
                # Carry the last-run stamp across, or a turn that already ran today would
                # run again the moment it became an automation — the due-ness check reads
                # exactly this field.
                await automations.mark_run(
                    automation_id=automation.id,
                    status=turn.last_status or "ok",
                    ran_at=turn.last_run_at,
                )
            await turns.mark_run(
                turn_id=turn.id,
                status=MIGRATED_MARKER,
                ran_at=turn.last_run_at or automation.created_at,
            )
            moved += 1
            log.info(
                "scheduled turn migrated to an automation",
                turn=turn.id,
                automation=automation.id,
                tenant=turn.tenant,
            )
        except Exception as exc:  # one bad row must not block the rest
            log.warning("scheduled turn migration failed", turn=turn.id, error=str(exc))
    return moved


def _name_for(prompt: str) -> str:
    """A human name for a migrated turn, derived from its prompt.

    Scheduled turns had no name — the prompt was the whole identity — so one is invented
    here rather than left blank, which would render as an empty row on the Automations page.
    """
    first = prompt.strip().splitlines()[0] if prompt.strip() else "Scheduled turn"
    return first[:80] if len(first) <= 80 else f"{first[:77]}…"


__all__ = ["MIGRATED_MARKER", "migrate_scheduled_turns"]
