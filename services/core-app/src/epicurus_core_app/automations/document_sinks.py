"""The notes/kb sinks — a run's output routed into a module document (#672, ADR-0108).

The #541 rule (ADR-0101): there is **no second write path**. An automation that saves to notes or
knowledge writes through the *same* module document API the operator's own editor and an approved
suggestion use — :meth:`ModuleRegistry.save_page_doc` — so the artifact is indistinguishable from a
hand-authored one and inherits the module's indexing, version history, and events for free.

The routing is deterministic and post-run (the sink seam, ADR-0105): the model produced an answer,
and the automation's configured :class:`~epicurus_core_app.automations.model.DocumentTarget` decides
where it lands. ``create`` overwrites the target each run (a daily report keyed by ``{date}``);
``append`` accretes into one document (a running log). Each write returns an ``EntityRef`` the
runner records on the ledger, so the runs feed links what was produced.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from epicurus_core import EntityRef, get_logger
from epicurus_core_app.automations.model import (
    Automation,
    DocumentTarget,
    render_document_path,
)
from epicurus_core_app.automations.sinks import SinkHandler
from epicurus_core_app.scheduling import TimezoneProvider

log = get_logger("epicurus_core_app.automations.document_sinks")


class DocumentWriter(Protocol):
    """The slice of :class:`ModuleRegistry` a document sink needs (eases faking in tests)."""

    async def save_page_doc(
        self, name: str, page_id: str, path: str, content: str
    ) -> dict[str, Any]: ...

    async def get_page_doc(self, name: str, page_id: str, path: str) -> dict[str, Any]: ...


class SinkNotConfigured(Exception):
    """A notes/kb sink was enabled without a document target — a misconfiguration, not a crash.

    Raised so the dispatcher records the sink as *failed* (visible in the ledger) rather than
    silently doing nothing. ``validate_automation`` rejects this at write time, so it is a belt to
    that pair of braces for a row that predates the check or was written around the API.
    """


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name.strip() or "UTC")
    except Exception:  # unknown / blank tz — never fail a delivery over it
        return ZoneInfo("UTC")


def _ensure_md(path: str) -> str:
    """Notes and knowledge are markdown editors; give the path a ``.md`` suffix if it lacks one."""
    path = path.strip().lstrip("/")
    return path if path.lower().endswith(".md") else f"{path}.md"


def _basename(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name[:-3] if name.lower().endswith(".md") else name


def _appended(existing: str, output: str, now: datetime) -> str:
    """Existing content plus a timestamped new entry — the ``append`` shape."""
    stamp = now.strftime("%Y-%m-%d %H:%M")
    entry = f"## {stamp}\n\n{output.strip()}\n"
    if not existing.strip():
        return entry
    return f"{existing.rstrip()}\n\n{entry}"


def make_document_sink(
    *,
    writer: DocumentWriter,
    module: str,
    page_id: str,
    get_target: Callable[[Automation], DocumentTarget | None],
    timezone: TimezoneProvider,
) -> SinkHandler:
    """Build the sink handler for one document module (notes or knowledge).

    Closed over the registry (as :class:`DocumentWriter`), the module + page it writes to, the
    per-automation target lookup, and the operator's timezone (so ``{date}`` in a path pattern is
    the operator's local date, matching the schedule vocabulary). Returns an ``EntityRef`` for the
    document written, which the dispatcher collects onto the run's ledger entry.
    """

    async def handler(automation: Automation, output: str) -> EntityRef | None:
        target = get_target(automation)
        if target is None or not target.path_pattern.strip():
            raise SinkNotConfigured(f"{module} sink enabled with no document target")
        now = datetime.now(_zone(await timezone()))
        path = _ensure_md(render_document_path(target.path_pattern, now=now))
        content = output
        if target.mode == "append":
            existing = await _read_existing(writer, module, page_id, path)
            content = _appended(existing, output, now)
        await writer.save_page_doc(module, page_id, path, content)
        log.info(
            "automation document sink wrote",
            module=module,
            path=path,
            mode=target.mode,
            automation=automation.id,
            tenant=automation.tenant,
        )
        return EntityRef(ref_id=path, module=module, kind="document", title=_basename(path))

    return handler


async def _read_existing(writer: DocumentWriter, module: str, page_id: str, path: str) -> str:
    """The document's current content, or ``""`` when it does not exist yet (append→create)."""
    try:
        data = await writer.get_page_doc(module, page_id, path)
    except Exception:  # 404 / unreachable — treat as "nothing there yet", so append starts fresh
        return ""
    content = data.get("content")
    return content if isinstance(content, str) else ""


#: The document modules a sink can target, and the editor page each writes into.
NOTES_MODULE = "notes"
NOTES_PAGE = "notes"
KNOWLEDGE_MODULE = "knowledge"
KNOWLEDGE_PAGE = "vault"


def make_notes_sink(writer: DocumentWriter, timezone: TimezoneProvider) -> SinkHandler:
    """The ``notes`` sink: writes to the notes module's editor (#672)."""
    return make_document_sink(
        writer=writer,
        module=NOTES_MODULE,
        page_id=NOTES_PAGE,
        get_target=lambda a: a.notes_target,
        timezone=timezone,
    )


def make_kb_sink(writer: DocumentWriter, timezone: TimezoneProvider) -> SinkHandler:
    """The ``kb`` sink: writes to the knowledge module's vault editor (#672)."""
    return make_document_sink(
        writer=writer,
        module=KNOWLEDGE_MODULE,
        page_id=KNOWLEDGE_PAGE,
        get_target=lambda a: a.kb_target,
        timezone=timezone,
    )


__all__ = [
    "DocumentWriter",
    "SinkNotConfigured",
    "make_document_sink",
    "make_kb_sink",
    "make_notes_sink",
]
