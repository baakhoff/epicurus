# tasks — provider-neutral task management

**`epicurus-tasks`** is a sidecar module that manages tasks via a swappable provider
back-end (ADR-0016). The agent interacts with one stable tool surface regardless of
which provider is active. **v0.1 ships two providers:**

- **`local`** (default) — tasks stored in the module's own tenant-scoped Postgres
  table. Works with no external account.
- **`google`** — tasks in Google Tasks, via the Google Tasks REST API. Token is
  fetched from the core's OAuth vault; no credential lives in this module.

Post-v0.1: add Todoist, Microsoft To Do, or any other provider without reshaping the
tool surface. Host port **8091**.

**v0.2.0** adds a **Tasks** left-nav page — a core-rendered `board` of open tasks grouped
by due date, where the user completes, edits, and adds tasks — and the `tasks_update` tool
that backs editing (ADR-0018). The module supplies data only; the shell renders it.

**v0.3.0** makes the module a **chat-attachment source** (ADR-0019): a task can be picked in
the composer's attach menu and the agent uses it as explicit context for the turn. The module
serves the picker and resolve over its open tasks (see *Chat-attachment source*, below); the
core attach menu renders it.

**v0.4.0** makes agent-referenced tasks **entity-reference chips** (ADR-0019): `tasks_list`
returns its tasks as chips, hovering one shows the core **hover-card** (due date, status) and
clicking opens it in the right-panel `entity-detail` view. The module declares `resolver` and
serves `GET /resolve/task/{id}`; the core renders the chip, the hover-card, and the panel.

**v0.6.0** adopts the **account/collection model** (ADR-0030): there is no `TASKS_PROVIDER`
dropdown any more. The module holds both backends at once — the silent **local** default plus
**Google** — and routes to the task list the operator selects in the core-rendered
connected-accounts section. A `TasksRouter` (`router.py`) resolves the selection from the core's
stored prefs and falls back to local if the core is unreachable.

**v0.8.0** makes tasks **`multi`** — **each enabled list is a category** (ADR-0036, refining
ADR-0030's single-active for tasks). The board **aggregates open tasks across every enabled
list**, tagging each card with the list it came from; the **Add task** form offers a **list
picker** (the enabled writable lists, by name) so the operator chooses the category per task,
and each card's Complete / Edit routes back to the list the task belongs to. A single failing
list is skipped, not fatal (#209). Reads fall back to the local store only when no list is
enabled. The router stamps each task with its `list_id` / `list_title`; titles come from each
account's discovery (a lookup failure degrades to the list id, never failing the board).

**v0.9.0** lets the operator **pick a list when adding from chat and move tasks between
lists** (ADR-0038, #257). A new **`tasks_lists`** tool reports the available lists so the
agent can ask which one (it previously had no way to see them); `tasks_update` gains a
**`to_list_id`** move target, and the board's Edit form gains a **List** picker (shown with
≥2 writable lists). Google Tasks has no cross-list move API, so a move **recreates** the task
in the target list and deletes the source — it gets a new id, and subtasks/order aren't
carried. Moves operate between external lists (the local "Personal" store is the silent
default and only shows when no external list is enabled).

**v0.10.0** gives the board **view controls** (ADR-0049, #298): the operator can change how
tasks are laid out and surface completed ones. A **Group by** control switches the column
layout — **Due date** (default), **Status**, **Priority**, **List** (when there are named
lists), or **None** (a single flat list) — and a **Show** filter chooses the task scope —
**Open** (default), **Completed**, or **All**. The controls are declared in the board data
(`controls`) and rendered by the shell as a toolbar; selecting one re-fetches the page with a
forwarded query param (`group` / `show`), so grouping and filtering stay module-side with no
core change. Completed cards are struck through and offer **Reopen** in place of **Complete**.
The provider read seam gains a `scope` (`open` / `done` / `all`) so completed tasks can be
fetched (local filters the `completed` flag; Google sets `showCompleted`/`showHidden`).

**v0.11.0** adds **task deletion** (#336) and a tidier add affordance (#337). A new
**`tasks_delete`** tool (agent-usable) and a per-card **Delete** board action (operator,
confirm-gated) both route to the provider's existing `delete_task` via the `TasksRouter` — one
delete path, validated against the manifest. The bulky **Add task** toolbar button becomes a
compact icon-only **"+"** via a new `icon_only` board-action hint the shell renders (the label
moves to a tooltip + `aria-label`).

**v0.12.0** fixes `tasks_update` phantom updates (#475): an agent asked to clear a task's due
date could call `tasks_update` repeatedly, see success every time, and change nothing. Three
fixes. **Clear sentinel** — `due=""` and `notes=""` now explicitly unset the field (Google
receives a PATCH `null`; the local store writes `NULL`), distinct from omitting the field
(`None`), which still means "leave unchanged." **No-op rejection** — `tasks_update` with no
mutable field and no `to_list_id` raises an actionable error instead of silently succeeding (the
Google provider still GETs-and-returns on a field-less call at the provider layer — a safe
low-level fallback — but the tool the agent and board actually call never reaches it emptyhanded
now). **Cross-list resolution** — `complete_task` / `update_task` / `delete_task` with no
`list_id` now search the operator's lists (active → other enabled → local, the same order
`get_task` already used) via `TasksRouter._locate_task` instead of assuming the default write
target, so a mutation reaches a task that lives in a non-default list instead of 404ing there.

**v0.13.0** adds **creating a task list** (#474) — previously only possible outside epicurus, in
Google Tasks' own UI. A new **`create_list`** provider seam, a **`tasks_create_list(title)`**
MCP tool, and a board-level **New list** action (shown whenever the Add form's list picker is,
i.e. once an external account is connected — ADR-0031 auto-enables its lists on connect) all
route through the `TasksRouter` to the sole configured external provider — **Google only**: the
local store is a single implicit list by design (ADR-0030) and has no lists of its own to
create, so `LocalTasksProvider.create_list` raises `NotImplementedError` rather than pretending
to support it. The returned list id is immediately usable as `list_id` on `tasks_add` or
`to_list_id` on `tasks_update`, but — like any newly discovered Google list — it needs the
operator's one-time toggle in the connected-accounts Lists section before it appears as a board
category or in `tasks_lists`; the module has no path to write the operator's collection prefs
itself, so it cannot auto-enable what it just created (a natural follow-up if wanted). Renaming
and deleting a list are deliberately out of scope (destructive; need a policy for the tasks
inside).

**v0.14.0** adds **recurring tasks** (#471, ADR-0082) — on both providers, even though the
Google Tasks API **has no recurrence field** (repeat is UI-only). A task carries an optional
`repeat` rule (a bare RFC 5545 RRULE, e.g. `FREQ=WEEKLY`); **completing it materializes the next
instance** with the next due date and retires the rule on the completed one, so the recurrence
lives on exactly one open task at a time — re-completing can't double-fire and a `COUNT`/`UNTIL`
series ends cleanly. The rule is stored per provider — a `repeat` column on the local row, a
module-owned `task_repeats` side table keyed by task id for Google — but materialization is
provider-agnostic (it lives in the `TasksRouter.complete_task`). `tasks_add`/`tasks_update` gain
a `repeat` parameter (a `due` is required to anchor it, and the RRULE is validated at the tool
boundary); the board card shows a **Repeats weekly** badge and the hover-card a **Repeat** row.
The web form renders `repeat` as a **friendly repeat picker** (the shared `format: rrule` widget)
rather than a raw RRULE box — the agent tool still takes a raw RRULE. The next due date uses a
**skip-missed** policy: a task completed late rolls forward to the next *future* occurrence, not
an already-overdue one. Month-end rules follow RFC 5545: a monthly task due on the 31st **skips**
months without a 31st (Feb, Apr, …) rather than clamping to their last day — the next occurrence
after Jan 31 is Mar 31. Google caveats: the rule is invisible in Google's own UI; a task changed
directly in Google is reconciled on our next refresh; deleting it in Google retires the rule (GC
on miss).

**v0.15.0** closes out the #471 follow-ups (#515):

- **Overdue sweep.** Materialization was on-complete only — a recurring task nobody ever
  completed just sat overdue forever. Every read (`tasks_list`, the board) now also
  materializes a fresh instance for any open, overdue recurring task: the overdue task itself
  stays open and untouched (still the operator's call whether to still do it, or delete it) —
  only its *rule* retires, moving the recurrence to a new successor due on the next occurrence
  (skip-missed, exactly like a late completion). Lazy and on-read by design, not a periodic
  background job — simpler, and reads are frequent enough that staleness is bounded to "until
  the next read."
- **Materialize failure paths.** A failure computing the next due date, creating the successor,
  or retiring the source's rule is logged and never breaks the completion or read that
  triggered it — a genuine parse/write issue leaves the rule in place so it can be retried
  rather than silently dropping the recurrence. The one dangerous case — the successor already
  exists but retiring the source's rule fails — gets one retry before being logged at error
  level and given up on; the residual risk (a duplicate successor on the *next*
  completion/sweep) is accepted rather than adding an unbounded retry loop, and the operator can
  always clear a stray rule by hand.
- **Due-less repeat now rejects.** `tasks_add` already required a `due` to anchor a repeat rule;
  `tasks_update` gained the same check — setting `repeat` on a task with no due (and none
  supplied in the same call) raises instead of silently storing a rule that can never
  materialize.
- **Board-form clearing.** The shared `SchemaForm` (tasks and calendar) previously dropped
  *any* blanked optional field from the submission, so "Does not repeat" (or clearing due/notes)
  from the board's edit form could never reach the tool — indistinguishable from leaving the
  field alone. It now sends an explicit `""` when a field that *had* a value is blanked, while a
  field left blank throughout is still omitted.

**v0.15.1** hardens the v0.15.0 sweep, closing four follow-ups from its own merge review
(#533, #534, #535, #539):

- **Due clear now rejects on a live rule.** The symmetric case to the due-less-repeat check
  above: `tasks_update(due="")` with `repeat` left untouched now rejects when the task's rule
  is currently live — clearing the anchor would otherwise strand the recurrence exactly like
  the due-less case (the sweep skips a due-less task, and a later completion has no due date
  to compute a next occurrence from). Clearing both `due=""` and `repeat=""` together in the
  same call is unaffected — that's an intentional "end the series."
- **Overdue-sweep concurrency guard.** Two holes in the on-read sweep: (a) two simultaneous
  `tasks_list` reads (e.g. the board and a chat turn) could both materialize the same overdue
  anchor with no lock, and (b) a persistently failing retire spawned a fresh duplicate on
  *every* subsequent read instead of failing once. Both are closed by a small in-process claim
  the router holds per `(tenant_id, task_id)` while materializing — best-effort (it narrows the
  race within one running instance rather than guaranteeing it across replicas), which is a
  proportionate trade for a failure mode the original review already characterized as rare.
- **Operator-timezone recurrence clock.** The overdue sweep and materialization compute
  "today" via the tenant's configured IANA timezone (ADR-0039) now, not UTC — mirroring
  calendar's `_resolve_timezone` (#433). A UTC-negative operator previously had a task swept
  (its rule retired, a successor spawned) up to the timezone offset hours early; UTC-positive,
  late. Falls back to UTC if the core is unreachable or the zone name is unrecognized.
- **`tasks_list` adopts the shared listing cap.** Matches calendar's `capped_listing`
  adoption (#522/#468) for consistency — a long backlog no longer inflates the tool's text
  with an unbounded number of lines; the entity-reference chips stay uncapped regardless.

**v0.15.2** finishes the v0.15.1 timezone work at the board's **display** call site (#555).
The overdue sweep already computed "today" in the operator's zone (v0.15.1 above), but the
board page still grouped its **Today / Overdue** columns by the UTC day — so within a single
render the two clocks could disagree across the UTC/operator midnight: a task the sweep counted
as due today could sit in the board's Overdue column. The page now reads the same
operator-timezone clock the sweep runs on (one shared clock, `operator_clock`), so the grouping
and the sweep always agree. Core unreachable → both degrade to UTC together. (The extra
per-render timezone lookup this introduces is deduplicated by a short-TTL memo added alongside
the sweep's cancellation hardening, #553.)

**v0.15.3** hardens the sweep's materialize machinery further and caches the timezone read (#553):

- **The materialize claim no longer leaks on cancellation.** `_materialize` claims a
  `(tenant_id, task_id)` key before spawning a successor and releases it at each normal outcome —
  but every handler was `except Exception`, which does **not** catch `asyncio.CancelledError` (a
  `BaseException` since 3.8). A cancel between claim and release (client disconnect, core timeout)
  left the key claimed forever, so that task's recurrence silently never materialized again until a
  process restart. The claimed region now runs under an `except BaseException` that releases the
  claim **synchronously** (never awaiting on a cancellation path) and re-raises — *unless* a
  successor was already created but its rule not yet retired, which is the intentional
  keep-claimed terminal state (#533b) that stops duplicate amplification.
- **"Today" is resolved once per read, before claiming.** The operator timezone was fetched
  uncached on the hot path — once per enabled collection in the sweep and again per materialized
  anchor, each a fresh `get_timezone` HTTP round trip inside the claimed window. It is now
  resolved once at the top of `list_tasks` (and once per completion) and threaded into the sweep
  and `_materialize`: one core call per read regardless of collection count, a claimed window of
  pure DB work, and one "today" the sweep and materialize can't disagree on across a midnight tick.
- **The operator-timezone clock memoizes for 60 s.** `operator_clock` caches the resolved zone
  for a short TTL so reads seconds apart (the board render + a chat turn) share one `get_timezone`
  call, and a core-down spell logs one warning per window instead of one per call.
- **`tasks_list` header wording restored.** The `capped_listing` adoption (v0.15.1) changed the
  pre-cap header from "Found N open task(s):" to "Found N task(s):"; `noun="open task"` restores
  it — `tasks_list` only ever returns open tasks.

**v0.16.0** shows task due-dates on the **calendar page** (#469, ADR-0088) — "what's on my
plate today" without splitting attention between the tasks board and the calendar. Serves a new
`GET /calendar-feed?start=&end=` (open tasks due in range, `end` exclusive); this is **not** a
manifest-declared capability — the core's aggregator probes every enabled module for the path and
skips a 404, so opting in is just serving it, no flag to set (see *Calendar-feed*, below, and
[core-app](core-app.md)). Every task hover-card (not only calendar-originated ones) also gains an
`href` back to the Tasks board, previously absent.

**v0.19.0** adds **the Can** (#766) — a second left-nav page holding the backlog: every task
**without a due date**, in one flat column. The board and the Can **partition** the same fetch
by `due` alone: the board now shows *scheduled* (dated) tasks only, under **every** grouping —
the due grouping's "No date" bucket is gone — so it is always a clean picture of what's
actually on the calendar, and the Can holds everything else. Scheduling is one tap: each Can
card leads with a **Schedule** action (a due-only `tasks_update` form, prefilled to today,
rendered as the SchemaForm date picker via a new `format: "date"` hint on both tools' `due`
params); clearing a task's due date moves it back to the Can. The Can's **Add** creates a task
with no due field offered (and no `repeat` — a rule needs a due anchor); its only view control
is **Show** (open / completed / all), so completed backlog items stay reachable. Nothing
vanishes silently: `tasks_add`'s tool description and both `due` param descriptions say an
undated task files into the Can — the same text doubles as the web form's field hint — and a
task's hover-card `href` now points at the page it actually lives on (board or Can). Purely a
read-partition: no provider or DB change; dateless tasks from any provider (Google allows
them) appear in the Can, and lead-time notifications are untouched (they key on due dates,
which Can items don't have — by design). **Superseded by v0.23.0** (#820): the Can's second
left-nav page folded into the Tasks page's *Show → Backlog* option — the partition, the
Schedule flow, and the copy-honesty rule described above are unchanged; only the page moved,
and completed-item reachability moved from the Can's own Show filter to a muted Completed
section within the backlog (see v0.23.0 below).

**v0.20.0** gives the Tasks board **three representations of the same data** (#767) —
**Board** (kanban), **List** (sortable flat rows), and **Calendar** (a month grid placing
tasks by due date) — switchable in place. The module's part is declarative: a third view
control, **View**, using the board archetype's **reserved `view` control id** (a #767
extension of ADR-0049 — the shell renders the switcher and the representations; see
[modules.md](../reference/modules.md)); the `?view=` param is clamped/echoed like
`group`/`show`, and *Group by* is omitted off the Board view (grouping shapes kanban
columns — the flat/date-keyed views would render it as a dead knob) while *Show* applies
everywhere. Cards additionally carry their **structured fields** — `due` (bare ISO date),
`priority`, `tags`, `list_title` — as data beside the rendered badges, which is what the
List sorts by and the Calendar places by. The payload is identical across views; the chosen
view persists per page shell-side (localStorage), with a `?view=` deep-link winning. (At the
time of this release the Can kept no View control at all, being its own page — since
superseded by v0.23.0, where the folded-in backlog keeps Board/List but the Calendar view has
nothing dated to place, see below.) The cross-module calendar *feed* (#469, v0.16.0 above) is
untouched — this is a
tasks-page-local representation, not a replacement for the calendar page's overlay.

**v0.21.0** finishes wiring **tags** into the board UX (#763) — the model and tools carried
`tags` since v0.5.0, but the surface was half-built (a bare comma-separated text input; no
grouping). Three pieces. **Group by → Tags**: offered exactly like *List* — only when a
visible (dated) task actually has a tag — it is the board's first **multi-membership**
grouping: a task appears under **each** of its tags, untagged tasks land in an **Untagged**
column, and columns sort alphabetically (case-insensitive, Untagged last, stable across
reloads). **Chips input**: the add/edit forms' `tags` field now declares `format: "tags"`
(the ADR-0082 seam), so the shell renders removable chips + a typeahead; the module supplies
its distinct tags (across board *and* backlog) as `field_suggestions` — a new `field_choices`
sibling for open suggestions — and the submitted value stays the comma-separated string, so
the MCP contract is unchanged. **Google honesty**: tags are local-only (Google Tasks has no
such field and the provider ignores them on write), so the tags field is **hidden wherever
the write would land on Google** — a Google task's Edit form, and the Add forms whenever the
list picker shows (it lists external writable lists only) — rather than pretending to save;
a Google task groups under Untagged. Drag-and-drop stays honest under the new grouping: list
columns now carry their `list_id`, the shell matches drop targets **by id** (never by a
display title a tag column might share), and with no list columns on screen cards aren't
draggable at all — drag-to-retag is deliberately not implemented in v1.

**v0.22.0** fixes a stale-account trapdoor: disabling a connected account could make the
Tasks page go silently blank while the agent kept reporting success (#795). `TasksRouter`
resolved a **missing provider** — a `prefs.enabled`/`prefs.active` ref whose account had
since been disconnected, which nothing prunes when that happens — oppositely on write and
read: `add_task`'s default-target resolution fell back to the local store, but the read
aggregate's `continue` skipped the same ref and never tried local at all, so a task written
through the stale ref was created, persisted, and permanently invisible through the router.
**The fix**: every read and write path now resolves a ref through one function,
`TasksRouter._resolve_provider` — a live provider is used as-is; a missing one **degrades to
local**, logging a warning that names the account and collection (previously nothing logged
at all). A `_dedup_refs` helper applies this across a multi-ref read (the aggregate
`list_tasks`, and the active→enabled→local task search behind `get_task`/mutations) so two
independently stale refs — or a degraded ref landing on a local entry already present —
collapse to a single local read rather than double-counting. The explicit-`list_id` write
branch is unified the same way, closing a related (lower-severity, not independently
reachable with today's single external provider type) asymmetry where a stale id could fall
through to "the sole external provider owns this unlisted id" and misroute to an unrelated
account instead of degrading to local. Nothing about *pruning* `prefs.enabled` changed —
core-app's disconnect flow (`ModuleRegistry.disconnect_collections`) already best-effort
removes a disconnected provider's refs, but that cleanup can still fail silently (a module
hiccup mid-disconnect), and a ref can go stale by other paths a central prune can't fully
close either; making every read/write path tolerate a stale ref regardless of *why* it went
stale is the durable fix (ADR-0030's local-is-the-silent-default already implied this — the
router just didn't fully honor it).

**v0.23.0** folds **the Can into the Tasks page** (#820): the left nav goes back to one tasks
entry, and the backlog is reached via **Show → Backlog** on the Tasks page instead of a
second page. The Can's own `PageSpec` (and its `GET /pages/can` route) is gone —
`GET /pages/can` now 404s like any other unknown page id; the backlog's data now comes back
from `GET /pages/board?show=backlog`. `show` is clamped by the module (`coerce_show`) to
open/done/all/**backlog**: the first three are unchanged — they still fetch that
:class:`TaskScope` and render the dated board via `build_tasks_board` — and `backlog` is a
**page-level partition value, not a widened `TaskScope`**: the app branches on it *before*
the provider fetch (fetching every status, `scope="all"`) and renders the undated backlog via
a new `build_tasks_backlog`, which keeps the Can's shape — one flat column, an Add with no
due/repeat field, and each card's leading **Schedule** action. *Group by* is omitted whenever
`show=backlog` (a flat backlog has nothing to group, the same #767 dead-knob rule already
applied to List/Calendar), and the **Calendar** view drops `backlog` from Show's own options
entirely — a backlog has no due dates to place on a grid, so `coerce_show` also corrects a
stale or explicit `show=backlog` back to Open whenever `view=calendar`, keeping the Show
control's echoed value always inside its own offered options.

**The axis nuance.** Show used to mean two different things depending on the page: a *status
scope* (open/completed/all) on the board, and, independently, the Can's *own* Show filter
over the same three values applied to the backlog. Folded onto one control, only one Show
value can be active at a time, so the backlog can no longer carry that second, independent
status filter. The chosen rule: `build_tasks_backlog` always fetches every status and splits
on it internally — open **and** in-progress tasks lead in a flat **Backlog** column, and any
**completed** undated task follows in its own, visually muted **Completed** column (an
ordinary struck-through card, exactly like a completed dated card elsewhere) — never omitted.
This is the simplest rule that keeps the acceptance bar — **no undated task, open or
completed, becomes unreachable** — without inventing a second control. Every other undated
Can behavior (the partition itself, the Schedule round-trip, the per-card actions, the Google
tags honesty rule, #766) is unchanged; only the page moved and completed reachability moved
from a second Show filter to the muted Completed section. Copy sweep: agent- and
web-form-facing text now says "the backlog" / "Show → Backlog" instead of "the Can" — the
`tasks_add` tool description, both `due` param descriptions, and a task's hover-card `href`
(now `/m/tasks/board?show=backlog` for an undated task rather than a separate page's URL).

**v0.24.0** adds **tenant data portability** (#867/#871): the module declares
`portable=True` and serves `GET /export` / `POST /import` via `add_portability_routes`
(schema `tasks/1`), so the core's export/import orchestrator can carry local tasks, the
recurrence rule of a Google-linked task, and the operator's lead-time preference between
installs — see *Portability*, below.

## The contract it exposes

### MCP tools (agent-facing)

| Tool | Inputs | Returns |
| --- | --- | --- |
| `tasks_list(list_id?)` | `list_id`: optional list identifier (omit for default) | Open tasks as **entity-reference chips** (ADR-0019), newest first. |
| `tasks_lists()` | none | The available lists (categories) as `- <title> — id: <id>` text, so the agent can pick one (or report only the default list exists). |
| `tasks_create_list(title)` | `title`: the new list's display name | The created list as a `Collection` (`account`/`collection`/`title`/`writable`). **Google-only** (#474) — raises if no external account is connected; the local store has no lists of its own. |
| `tasks_add(title, notes?, due?, priority?, tags?, status?, list_id?, repeat?)` | `title`: required; rest optional. `due`: omit it and the task files into the **backlog** (Show → Backlog on the Tasks page, #766/#820) instead of the board — the tool description says so, so "note down: buy a drill" lands there knowingly. `list_id`: target list (from `tasks_lists`). `repeat`: RRULE making it recurring (needs a `due`) | The created `Task`. |
| `tasks_complete(task_id, list_id?)` | `task_id`: provider task ID; `list_id`: optional — omit to have it looked up across your lists | The updated `Task` with `completed=True`. A recurring task also spawns its next instance (ADR-0082). |
| `tasks_update(task_id, title?, notes?, due?, priority?, tags?, status?, list_id?, to_list_id?, repeat?)` | `task_id`: provider task ID; pass **at least one** mutable field or `to_list_id` — a field-less call raises. `due=""` / `notes=""` / `repeat=""` **clears** that field (`None`/omitted leaves it unchanged) — clearing `due` moves the task off the board into the **backlog** (#766/#820), and raises while a repeat rule is still live (#534; clear `repeat=""` too, or give it a new due, instead); setting a due date on a backlog task schedules it onto the board. `to_list_id`: **move** the task to this list; `repeat`: an RRULE (`""` makes it one-off) | The updated `Task` (the moved task on a move). |
| `tasks_delete(task_id, list_id?)` | `task_id`: provider task ID; `list_id`: optional — omit to have it looked up across your lists | A short confirmation string. **Permanent** — unlike `tasks_complete`, the task is removed. Idempotent on the local store (a missing id is a no-op). |

All tools are **provider-agnostic** (ADR-0030/0036). `tasks_list` with no `list_id`
**aggregates open tasks across every enabled list**; with a `list_id` it reads just that
list. `tasks_add` (a create) routes to the list named by `list_id`, or — with none — to the
default target (the active list, else the first enabled, else local). `tasks_complete` /
`tasks_update` / `tasks_delete` (mutations on an *existing* task) route to `list_id` when
given; with none, they **search** the same lists `get_task` does (active → other enabled →
local, #475) instead of assuming the default target, so a mutation still reaches a task that
lives in a different enabled list. Before adding from chat the agent should call
**`tasks_lists`** and ask which list when more than one exists. `tasks_update` edits content
(title/notes/due — `due=""` / `notes=""` clears one, #475) and rejects a call with nothing to
change; passing **`to_list_id`** moves the task (recreate+delete on Google — ADR-0038);
`tasks_complete` flips the done flag — distinct operations. The `Task` domain model is:

```python
class Task(BaseModel):
    id: str
    title: str
    notes: str | None = None
    due: str | None = None  # ISO date, e.g. "2025-01-15"
    status: Literal["open", "in_progress", "done"] = "open"
    completed_at: str | None = None
    priority: Literal["low", "medium", "high"] | None = None  # local-only
    tags: list[str] = []  # local-only
    repeat: str | None = None  # RRULE if recurring (#471, ADR-0082)
    list_id: str | None = None  # the list (category) — router-stamped
    list_title: str | None = None  # its human label — router-stamped
    # `completed` is a computed alias: True when status == "done".
```

`list_id` / `list_title` are **not stored** — the router stamps them when it aggregates the
board so each card knows its category and routes its mutations to the owning list (ADR-0036).
`repeat` **is** persisted, but per provider: the local store keeps it in-row, and the Google
provider — Google Tasks has no recurrence field — keeps it in a module-owned `task_repeats`
side table keyed by task id (ADR-0082).

### HTTP

| Endpoint | Description |
| --- | --- |
| `GET /health` | Liveness probe. |
| `GET /metrics` | Prometheus metrics. |
| `GET /manifest` | Module manifest (tools, UI declaration, `collections` spec). |
| `GET /status` | `{"google_connected": bool}` (best-effort live OAuth check). |
| `GET /accounts` | Connected accounts + their task lists for the picker (ADR-0030). The core proxies + merges this at `GET /platform/v1/modules/tasks/collections`. |
| `GET /pages/{id}` | Page data for the one manifest-declared page, `board` (the Tasks page); a `board`-archetype payload the core proxies (ADR-0018). `?show` (open/done/all/**backlog**, #766/#820) picks the dated board or the undated backlog partition; `?group` (due/status/priority/list/none, omitted under `show=backlog`) and `?view` (board/list/calendar, #767 — `backlog` drops off Show's own options under `view=calendar`) round out the view controls (ADR-0049), each clamped to a known value. 404 for an unknown id — including `can`, whose route retired with the page it served. |
| `GET /attachments` | Chat-attachment picker (ADR-0019): open tasks as `{ref_id, kind, title}`. Core-proxied. |
| `GET /attachments/{ref_id}` | Resolve an attached task to `{title, excerpt}` (ADR-0019); missing task is `404`. Core-proxied. |
| `GET /resolve/{kind}/{ref_id}` | Hover-card resolver for a referenced task (ADR-0019); `kind` is `task`. Returns a `HoverCard`; unknown kind / missing task is `404`. Core-proxied. |
| `GET /export?tenant_id=` | Tenant data portability (#867/#871): NDJSON export of this tenant's source-of-truth records (schema `tasks/1`) — see *Portability*, below. |
| `POST /import?tenant_id=&dry_run=` | Apply an NDJSON export back — upsert by stable id, never deletes. `409` on a newer or foreign schema; see *Portability*, below. |
| `GET /calendar-feed?start=&end=` | Open tasks with a due date in `[start, end)` (`end` exclusive), as calendar-feed items (#469, ADR-0088): `{id, title, date, status, ref_id, kind}`. No manifest declaration — probed generically by the core's cross-module aggregate, `GET /platform/v1/calendar-feed` (see [core-app](core-app.md)). |
| `GET /mcp` (streamable-HTTP) | MCP tool surface (served by the MCP SDK's `MCPServer`). |

### Web UI (manifest, ADR-0007 Tier 1)

| Panel | What it shows / does |
| --- | --- |
| **Status** | Whether Google is connected (polled from `GET /status`). |
| **Lists** | Connected accounts + their task lists: per-list on/off toggles and a **default** picker for new tasks (ADR-0030/0036). |
| **Actions** | None — `tasks_list` returns entity-reference chips (surfaced in chat), so it is not a card-action button. |
| **Tasks page** | A left-nav `board` page of *scheduled* (dated) tasks, with the undated backlog one Show selection away (**Show → Backlog**, #766/#820 — see below). |

### The Tasks page — `board` archetype (ADR-0018, #766/#820)

The module declares one page — `{id: "board", title: "Tasks"}`, `archetype: "board"` — and
serves its data at `GET /pages/board`. The core renders it; the module ships **no markup**.
The **Show** control **partitions every task by whether it has a due date**, among other
things: `open` / `done` / `all` render the board's dated tasks (under every grouping) and
`backlog` renders the undated ones instead, so a task lives behind exactly one Show value
and moves between them purely by gaining or losing a due date. The partition is read-side
only — no provider contract change; an undated task from any provider shows in the backlog.
(Formerly two pages — `board` and a second `can` — until #820 folded the Can into this one
Show option; `GET /pages/can` now 404s like any other unknown id.)

- **Columns** group the tasks **aggregated across every enabled list** by the operator's
  chosen **Group by** dimension (ADR-0049): **Due date** (default — Overdue / Today / Upcoming;
  the "No date" bucket is gone, #766 — undated tasks live in the backlog), **Status**, **Priority**,
  **List** (one column per category), **Tags** (#763 — offered only when a visible task has a
  tag; **multi-membership**: a task appears under each of its tags, untagged ones under
  **Untagged**, columns alphabetical with Untagged last), or **None** (a single flat list).
  Empty columns are dropped, and each card carries a **category tag** naming the list it came
  from (ADR-0036). List-grouped columns carry their `list_id`, which is what the shell's
  drag-move matches drop targets by (#763) — a tag column sharing a list's display name is
  never a target, and with no list columns cards aren't draggable at all.
  Layout is a pure function,
  `build_tasks_board(tasks, today=…, group_by=…, scope=…, lists=…, default_list_id=…)`, so it
  is unit-tested without a clock — ISO date strings compare lexicographically, no parsing; it
  drops undated tasks itself, so no caller can leak backlog onto the board. The
  `today` the columns key off is the **operator's** day, resolved from the same operator-timezone
  clock the overdue sweep runs on, so the Today/Overdue split and the sweep never disagree within
  a render (#555); it degrades to UTC when the core is unreachable.
- **View controls** (`controls` in the board data) are a **View** switcher (#767 — Board /
  List / Calendar, the archetype's reserved `view` control; the shell renders the segmented
  switcher, the alternate representations, and the per-page persistence — see
  [modules.md](../reference/modules.md)), a **Group by** selector, and a **Show** filter —
  **Open / Completed / All / Backlog** (#820, the fourth value folding in the former Can
  page) — rendered by the shell as a toolbar; changing one re-fetches the page with a
  forwarded query param (`view` / `group` / `show`, each clamped to a known value). *Group
  by* is offered only under the Board view **and** only when Show isn't Backlog — grouping
  shapes kanban columns, and a flat backlog has nothing to group either way (#767's
  dead-knob rule, extended in #820). Open/Completed/All choose the **scope** the providers
  read (`open` / `done` / `all`) and apply under every view, so the operator can review
  completed work; **Backlog** instead branches to the undated partition (fetched at every
  status internally — see below) *before* the provider fetch, rather than widening that
  scope. A backlog has no due dates to place on a grid, so the **Calendar** view drops
  `backlog` from Show's own options entirely, and the module corrects a stale or explicit
  `show=backlog` back to Open whenever `view=calendar` — the echoed Show value is always
  inside its own offered options. Completing an open task removes it from the open view;
  in the Completed/All views a completed card is struck through (`done: true`) and offers
  **Reopen** (`tasks_update status=open`) in place of **Complete**. Each card also carries
  its structured `due` / `priority` / `tags` / `list_title` fields as data (#767) — what the
  List view sorts by and the Calendar view places by.
- **Mutations are declarative actions** that name an MCP tool; the shell invokes it through
  the core (validated against the manifest) and refetches. Each card offers **Complete**
  (`tasks_complete`, one-tap), **Edit** (`tasks_update`, a form prefilled from the card), and
  **Delete** (`tasks_delete`, a `danger` action gated behind a confirm dialog, #336) — all
  carrying the task's `list_id` so the mutation routes to the **owning** list; the board
  offers **Add task** (`tasks_add`, a form) whose **list picker** chooses the target list
  (a labeled `field_choices` entry, value = list id → label = title). The forms' `tags`
  field renders as a **chips input** (`format: "tags"`, #763) with a typeahead over the
  module's known tags (`field_suggestions`); it is offered only where the write lands on
  the **local** store — Google Tasks silently drops tags, so a Google task's Edit form and
  any Add form showing the (external-only) list picker hide the field instead of
  pretending to save it. With **two or more**
  writable lists the Edit form also gains a **List** picker bound to `to_list_id` (prefilled
  to the task's current list); choosing another **moves** the task there — a recreate+delete
  on Google (ADR-0038). The **Add task** action sets `icon_only: true` so the shell renders it
  as a compact **"+"** with a tooltip label (#337). A board-level **New list** action
  (`tasks_create_list`, a form with a single `title` field) appears alongside it whenever the
  list picker does — Google-only (#474); creating one still needs the operator's one-time
  enable toggle before it shows up as a category (see the version note above). The board never
  carries credentials or business logic — it is data plus tool references.
- **The backlog** (`show=backlog`, #766/#820 — formerly the standalone Can page) is the
  undated tasks, built by the pure
  `build_tasks_backlog(tasks, today=…, view=…, lists=…, default_list_id=…)` from the **same
  fetched list** (`scope="all"`) the board partitions. **The axis nuance**: Show used to mean
  status scope on the board *and*, independently, the Can page's own Show filter over the
  backlog — folded onto one control, only one Show value is active at a time, so the backlog
  can't carry that second filter any more. The chosen rule instead splits the backlog by
  status internally into up to two columns: open **and** in-progress tasks lead in a flat
  **Backlog** column, and any **completed** undated task follows in its own, visually muted
  **Completed** column (an ordinary struck-through card) — either column dropped when empty,
  neither ever omitted. This is the simplest rule that keeps a completed undated task
  reachable without a second control. Cards are otherwise the ordinary task cards (category
  badge, Complete/Reopen, Edit with the move picker, Delete) plus a leading **Schedule**
  action — a due-only `tasks_update` form prefilled to today, rendered as the shell's native
  date picker via the `format: "date"` hint — which is how a task is placed on the board;
  clearing the due date (from any Edit form or the agent) sends it back. Its **Add** offers
  no due or `repeat` field (a rule needs a due anchor), so new entries land in the backlog by
  construction. The board's own Add still accepts an empty due; the `due` field's hint (the
  tool parameter description) says the task is then saved to the backlog, so nothing vanishes
  silently.

### Connected accounts & collections (ADR-0030)

The module declares `collections = {noun: "list", multi: true, providers: ["google"]}` and
serves **`GET /accounts`**: one account per supported provider, `connected` from the live OAuth
state and, when connected, its task lists (`{account, collection, title, writable}`). `local`
is never listed — it is the silent default.

The core merges this with the stored selection at
`GET /platform/v1/modules/tasks/collections`; the shell renders per-list on/off toggles plus a
**default** picker, and `PUT …/collections` persists `{enabled, active}` (`active` is the default
write target). The module reads it via `PlatformClient.get_collections()` (a Postgres-only read
at `GET …/collections/prefs`) and, being **`multi`**, **aggregates the board across every enabled
list** while routing each write/mutation to the list named by `list_id` (or the default — active,
else first enabled, else local).

**Resolution rule (`TasksRouter._resolve_provider`, #795, v0.22.0):** every read and write
path resolves a `CollectionRef` to a provider through this one function. A live provider for
the ref's account is used as-is. Otherwise — the core is unreachable, or the ref names an
account that isn't (or is no longer) connected, most commonly a stale `enabled`/`active`
entry left behind once its account was disconnected — it **degrades to the local
collection**, logging a warning that names the account and collection. This makes the local
default genuinely silent end-to-end (ADR-0030): a write that falls back to local is always
found by the very next read, never "write succeeded, read empty." A multi-ref read (the
aggregated `list_tasks`, and the active→enabled→local search behind `get_task` and the
per-task mutations) de-duplicates by effective target, so more than one stale ref — or a
stale ref alongside local already being present — reads local exactly once, not once per
ref.

### Entity references & hover-cards (ADR-0019)

`tasks_list` returns its open tasks as **entity-reference chips** rather than a bare list: each
chip carries the task id (`kind = "task"`, `module = "tasks"`), so the agent can refer to a task
later without re-listing. Hovering a chip fetches the task's **hover-card**; clicking opens it in
the right panel's `entity-detail` view. The module supplies data only — the core renders both.
(Because the list tool now returns a chip envelope rather than plain text, it is no longer a
module-card action button — tasks are surfaced through chat.)

**Resolver** (`resolver = true`) — `GET /resolve/task/{ref_id}` returns the uniform `HoverCard`
envelope (`title` · `description` · `details: [{label, value}]`): the task's notes as the
description, plus **Due** (when set) and **Status** (Open / Completed) detail rows. An unknown
`kind` or a missing task is a `404`. The core proxies it at
`GET /platform/v1/modules/tasks/resolve/{kind}/{ref_id}`. Every task hover-card also carries an
`href` back to where the task lives — `/m/tasks/board` for a dated task, or
`/m/tasks/board?show=backlog` for an undated one (#469/#766; one page and a query string since
#820, previously a separate `/m/tasks/can`) — added so a task reached from the calendar-feed
overlay (below) has a way back to it; the same link shows regardless of where the chip was
clicked (chat, the calendar, or elsewhere).

### Calendar-feed: task due-dates on the calendar page (#469, ADR-0088)

Open tasks with a due date show as read-only chips on the calendar page's due day, distinct from
real events — "what's on my plate today" without leaving the calendar. `tasks` is the first module
to implement the core's generic, **non-manifest-declared** `calendar_feed` convention (ADR-0088):
serving `GET /calendar-feed?start=&end=` is the only opt-in, no capability flag to set.
`calendar_feed_items(tasks, start, end)` (`epicurus_tasks.service`) filters the already-fetched
`scope="open"` list (open **and** in-progress, ADR-0049) to a `due` date inside `[start, end)`
(`end` exclusive), dropping undated tasks; each item's `status` reflects the task's own status,
not a flattened literal. `kind="task"` rides on every item so the shell's click handler can call
the generic resolver (above) without hardcoding "task" — a future second calendar-feed module
needs no web change. Completing or deleting a task removes its chip on the next range fetch (the
feed is computed fresh, not cached). See [core-app](core-app.md) for the aggregation side.

### Chat-attachment source (ADR-0019)

`attachable = true` — a task can be attached to a turn so the agent uses its details as
explicit context, beyond anything it would list itself:

- **Picker** — `GET /attachments` lists up to 50 **open** tasks as
  `{ref_id, kind: "task", title}` rows the composer shows.
- **Resolve** — `GET /attachments/{ref_id}` returns `{title, excerpt}` — the task's title,
  due date, status, and notes — which the agent injects into the turn's context.

Both are proxied by the core at `GET /platform/v1/modules/tasks/attachments[/{ref_id}]`; a
missing task is a `404`. They use the active provider's `get_task`, so they behave identically
against the local and Google backends. The picker offers the **default list** only (the core
attach proxy forwards no list selector).

## Provider detail

### `local` provider

- Tasks stored in `tasks_local` (Postgres), scoped by `tenant_id`.
- `list_id` is ignored — single flat list per tenant.
- Works out of the box with no operator setup beyond a running Postgres instance.
- `create_list` raises `NotImplementedError` — there is no list concept to add to (#474).

### `google` provider

- Calls the Google Tasks REST API (`tasks.googleapis.com`).
- OAuth token fetched from `GET /platform/v1/oauth/google/token` — **no client
  secret or refresh token lives in this module** (ADR-0020 / non-negotiable #8).
- `create_list` calls `POST /users/@me/lists` (`tasklists.insert`, #474).
- Requires the Google account to be connected via the Settings screen (issue #86
  OAuth flow) before any tool call can succeed.
- `list_id` defaults to `@default` (the user's default Google task list).
- Additional scopes required: `https://www.googleapis.com/auth/tasks`
  (requested at connect time via the incremental-scopes mechanism, issue #102).

## Module events (ADR-0103, #664)

Emitted on the module event spine (`epicurus_core.module_events.emit_event`), not a raw
`EventBus.publish` — see [the event catalog](../reference/events.md#the-event-catalog) for the
full envelope shape, payload discipline, and `dedup_key` convention. Base subject shown; the
wire subject gains the spine's `events.` prefix and is tenant-scoped.

| Event | Emitted from | Condition |
| --- | --- | --- |
| `tasks.task_created` | `TasksRouter.add_task` | A new task was created through this module. |
| `tasks.task_completed` | `TasksRouter.complete_task` | A task was marked done. |
| `tasks.task_updated` | `TasksRouter.update_task` | An existing task was edited (not a cross-list move — see `task_moved`). `dedup_key` includes a change hash of the task's mutable fields, same posture as `calendar.event_updated`: a genuinely different edit gets its own log entry; a retried write with identical resulting state dedups. |
| `tasks.task_moved` | `TasksRouter._move_task` | A task moved between lists (the ADR-0038/#257 cross-list seam — Google Tasks has no move API, so a move recreates in the target then deletes the source). Fires instead of `task_updated`, not alongside it. |
| `tasks.task_due_soon` | The lead-time scheduler (`epicurus_tasks.scheduler`) | An open task is within its configured lead time of its due date (tenant setting, default 1 day) — fires at most once per task via a durable marker. |
| `tasks.task_overdue` | The lead-time scheduler | An open task's due date has passed — fires at most once per task. |

**A recurring task's auto-materialized successor does not emit `task_created`.** The overdue
sweep and on-complete materialization (`TasksRouter._materialize`, ADR-0082) call the *inner*
provider's `add_task` directly, bypassing this router's own `add_task` — a known, deliberate
scope limit for this PR, not an oversight.

**The lead-time scheduler** (`epicurus_tasks.scheduler`) is tasks' first periodic background
job — the same pattern as calendar's new one (#664), a poll loop started/stopped with the app
lifespan, ticking every `SCHEDULER_POLL_INTERVAL_S` (default 300s — day-granular leads don't
need calendar's minute-level polling). Unlike calendar's pure-instant lead math, a task's `due`
is a **date**: "due within N days" is evaluated against the *operator's local calendar day*
(ADR-0039), reusing the exact `operator_clock` the overdue-recurrence sweep already resolves —
the scheduler and the sweep can never disagree about what day it is. Fire-once state lives in
the `tasks_fired_markers` table (below), keyed `(tenant, task_id, marker)` with a database
uniqueness constraint deciding races, not a read-then-write check — proven to survive a process
restart. The lead time itself is a tenant setting (`tasks_lead_time_prefs`, below) — storage
only in this PR, no operator-facing settings UI yet.

## Automation templates (#705, ADR-0105)

Two starter presets on the Templates tab — never auto-instantiated, see
[reference/automations.md#templates](../reference/automations.md#templates):

| Key | Trigger | Autonomy | Sinks |
| --- | --- | --- | --- |
| `due-today-digest` | Schedule: daily, 08:00 | `notify` | `push` |
| `on-task-overdue` | Event: `tasks.task_overdue` | `notify` | `push` |

Both need `tasks_list`/`tasks_lists` reachable at `notify` — the reason those two tools are
annotated `side_effect="read"`.

## Configuration

`TasksSettings` extends [`CoreSettings`](../reference/config.md). There is **no
`TASKS_PROVIDER`** any more (ADR-0030): the module always backs itself with the local store
and routes to the connected Google list the operator selects, which lives in the core
(`module_prefs`), not in service config.

| Env var | Default | Meaning |
| --- | --- | --- |
| `PLATFORM_URL` | `http://core-app:8080` | Core service URL for OAuth token, collection prefs, and platform API calls. |
| `DATABASE_URL` | `postgresql+asyncpg://…/epicurus` | Postgres DSN for the local default store. |
| `SCHEDULER_POLL_INTERVAL_S` | `300` | How often the lead-time scheduler ticks (#664) — `task_due_soon`/`task_overdue`. |

## Data model

### Local provider

- **Postgres `tasks_local`** — tenant-scoped task store:

| Column | Type | Description |
| --- | --- | --- |
| `pk` | `INTEGER` | Auto-increment primary key. |
| `id` | `VARCHAR(255)` | UUID task identifier (indexed). |
| `tenant_id` | `VARCHAR(63)` | Tenant scope (indexed). |
| `title` | `VARCHAR(1024)` | Task title. |
| `notes` | `TEXT \| NULL` | Optional free-text notes. |
| `due` | `VARCHAR(64) \| NULL` | Optional ISO date string. |
| `completed` | `BOOLEAN` | Whether the task is done. |
| `completed_at` | `VARCHAR(64) \| NULL` | ISO timestamp when completed. |
| `created_at` | `DATETIME` | Auto-set at insert time. |
| `status` | `VARCHAR(32) \| NULL` | `open` / `in_progress` / `done` (added v0.5.0). |
| `priority` | `VARCHAR(16) \| NULL` | `low` / `medium` / `high`; local-only (added v0.5.0). |
| `tags` | `TEXT \| NULL` | JSON array of labels; local-only (added v0.5.0). |
| `repeat` | `TEXT \| NULL` | RFC 5545 RRULE if recurring; local-only (added v0.14.0, #471). |

Unique constraint on `(tenant_id, id)`. A read's `scope` selects the rows by the `completed` flag (ADR-0049): `open` (the default, `completed = FALSE`), `done` (`completed = TRUE`), or `all` (no filter); all ordered by `created_at DESC`. `tasks_list` always reads the `open` scope; the board's *Show* filter passes the chosen scope.

Schema is created automatically by `TaskStore.init()` at startup, which also **reconciles
columns added after the table's first release** — there is no migration framework, so `init()`
runs `create_all` and then `ALTER TABLE … ADD COLUMN` for any model column missing from an
existing table (additive only; the v0.5.0 `status`/`priority`/`tags` fields and the v0.14.0
`repeat` field). Without this, a database provisioned before v0.5.0 has no `status` column and
**every** task read (the board, `tasks_list`, the attachment picker, the resolver) 500s with
`column tasks_local.status does not exist` (#247). Destructive changes — drops, renames, type
changes, `NOT NULL` backfills — still require a real migration.

- **Postgres `task_repeats`** (added v0.14.0, #471, ADR-0082) — emulated recurrence rules for
  **external-provider** tasks (Google has no recurrence field). Tenant-scoped, keyed by
  `(tenant_id, list_id, task_id)`; the local store keeps its own rule in `tasks_local.repeat`
  and never uses this table:

| Column | Type | Description |
| --- | --- | --- |
| `pk` | `INTEGER` | Auto-increment primary key. |
| `tenant_id` | `VARCHAR(63)` | Tenant scope (indexed). |
| `list_id` | `VARCHAR(255)` | The provider list the task lives in (e.g. `@default`). |
| `task_id` | `VARCHAR(255)` | The provider task id (indexed). |
| `rrule` | `TEXT` | The bare RFC 5545 RRULE. |
| `created_at` | `DATETIME` | Auto-set at insert time. |

Unique constraint on `(tenant_id, list_id, task_id)`. Created by the same `TaskStore.init()`
`create_all` (it shares the module's SQLAlchemy metadata). A row is written on `add_task`/
`update_task`, filled onto reads, and **retired** on `delete_task` or a `get_task` 404 (GC on
miss). Writes are delete-then-insert so they work identically on SQLite (tests) and Postgres.

### Google provider

No task persistence — tasks live in Google Tasks. The OAuth token is stored in
OpenBao by the core's OAuth subsystem under `oauth/tokens/google` (tenant-scoped). A repeating
Google task's rule is the exception: it lives in the module's `task_repeats` table above
(Google Tasks has no recurrence field), keyed by the provider list + task id (ADR-0082).

### Lead-time scheduler (#664)

Two more tables, in the same shared Postgres:

| Table | Scope | Holds |
| --- | --- | --- |
| `tasks_lead_time_prefs` | `(tenant)` PK | The `task_due_soon` lead time in days; `NULL`/missing falls back to `DEFAULT_LEAD_DAYS` (1). |
| `tasks_fired_markers` | `(tenant, task_id, marker)` unique | A fire-once claim — `task_id` is provider-qualified (`"local:<id>"` / `"google:<id>"`, inferred from `Task.list_id` the same way `TasksRouter._stamp` distinguishes local from external), `marker` is `"due_soon"` or `"overdue"`, `fired_at_ns` (`BigInteger`, nanosecond epoch) records when. A row's mere existence is the claim; a second insert attempt for the same key violates the unique constraint and is read as "already fired," not an error. |

## Portability (#867/#871)

The module implements `epicurus_core.PortabilityStore` (`portability.py`, `TasksPortability`)
and serves it via `add_portability_routes` (`GET /export` / `POST /import`, both tenant-scoped)
— the module half of tenant data export/import (ADR-0133). Schema **`tasks/1`**. Every kind is
upserted by its own stable id on import; nothing here ever deletes, and a second apply of the
same export reports everything `skipped`/`updated` with nothing duplicated.

| `kind` | Stable id | Columns that travel |
| --- | --- | --- |
| `task` | `tasks_local.id` (the task's own uuid — never the surrogate `pk`) | `title`, `notes`, `due`, `completed`, `completed_at`, `created_at` (ISO-8601), `status`, `priority`, `tags` (JSON array), `repeat`. |
| `task_repeat` | `"<list_id>:<task_id>"` — `task_repeats`' own natural composite key | `list_id`, `task_id`, `rrule`. |
| `lead_time_prefs` | a fixed id (`"prefs"`) — one record per tenant | `lead_days`. Exported only when the operator has set one explicitly; the unset default (`DEFAULT_LEAD_DAYS`) travels with the module's code, not the archive. |

**Excluded, and why:**

- **The Google task itself.** A connected Google task list lives in the operator's Google
  account, not this module's database — there is no mirror table to export. After import, the
  operator reconnects the account (as any OAuth-backed module requires) to see the tasks again;
  any already-imported `task_repeat` rows reattach to them by id the moment they do. This is why
  `task_repeat` travels even though the task it decorates does not — it is the *only* copy of
  that rule (Google Tasks has no recurrence field of its own, ADR-0082), so leaving it behind
  would silently drop a Google-linked task's recurrence on the new install.
- **`tasks_fired_markers`** (operational) — the lead-time scheduler's fire-once dedup state
  (#664). Re-firing a `task_due_soon`/`task_overdue` notification once on the new install is
  harmless (the operator hasn't seen it there yet); carrying stale markers over would instead
  risk *suppressing* a real one.
- **Collection selection** (the operator's enabled Google lists + active list) is core-side
  state (`module_prefs.collections`, ADR-0030), already carried by the core's own export —
  duplicating it here would just be two copies of the same fact to keep in sync.

No docker-dependent tests exist in this module (confirmed: no `testcontainers`/`docker`
reference anywhere under `services/tasks`); `tests/test_portability.py` is a plain unit suite
against a file-backed SQLite store, covering the export shape, the round trip (export → wipe →
import → equal), the idempotent second apply, `dry_run` writing nothing, an unknown `kind`
counting as skipped with a warning, tenant isolation, and the route's schema-compatibility
gate (same/older/newer/foreign, via `schema_verdict`).

## Dependencies

core-app (OAuth token endpoint) · Postgres (`local` provider + the `task_repeats` recurrence
side table + the lead-time scheduler tables, #664) · NATS (module event spine: `task_created`/
`task_completed`/`task_updated`/`task_moved`/`task_due_soon`/`task_overdue`) ·
`python-dateutil` (RRULE expansion for materialization, #471).

## Run & extend

```bash
# One container backs both local + Google; the operator picks the active list in the UI:
docker compose up -d tasks
```

To use Google: connect the account and pick a list in **Modules → Tasks → Lists** (or connect
from **Settings**). No restart or env change is needed (ADR-0030).

**Adding a new provider** — implement the `TasksProvider` Protocol (including `is_available`,
`list_collections`, and `create_list`) in a new file, add it to the `external` map in `app.py`
(keyed by its account id), and add it to `PROVIDER_LABELS` + `collections.providers` in
`service.py`. No tool or model changes are needed. If the new provider has no concept of
multiple lists, `create_list` can raise `NotImplementedError` like the local store does — the
router's own `create_list` only ever calls it on a genuine external provider, never on local.

Package `epicurus_tasks`:

| Module | Responsibility |
| --- | --- |
| `models.py` | `Task` domain model (provider-neutral). |
| `providers.py` | `TasksProvider` Protocol — the swappable back-end seam (list (by `scope`)/add/complete/update/delete + `get_task` + `is_available`/`list_collections`/`create_list`). |
| `local_provider.py` | `LocalTasksProvider` — Postgres-backed task store (the silent default); `create_list` raises `NotImplementedError` (#474) — a single implicit list has nothing to create. |
| `google_provider.py` | `GoogleTasksProvider` — Google Tasks REST API (+ list-discovery + delete + `create_list` via `tasklists.insert`, #474); persists/fills/GCs emulated recurrence rules via an injected `RepeatStore` (#471, ADR-0082). |
| `router.py` | `TasksRouter` — routes ops to the operator's active list across local + Google (ADR-0030); every read and write resolves a ref to a provider through the shared `_resolve_provider`, degrading a missing/disconnected one to local with a warning (#795), and a multi-ref read de-dupes through `_dedup_refs`; moves a task between lists by recreate+delete (ADR-0038); `_locate_task` resolves an existing-task mutation across lists when `list_id` is omitted (#475); `create_list` routes to the sole configured external provider (#474); `complete_task` **materializes** a recurring task's next instance via `_materialize_next` (#471, ADR-0082), and `list_tasks` sweeps overdue ones the same way (#515) — both funnel through the shared `_materialize`, guarded by an in-process `_claim_materialize`/`_release_materialize` pair against concurrent double-materialization and retire-failure amplification (#533); `operator_clock` resolves the sweep's "today" in the operator's timezone rather than UTC (#433, #535). |
| `recurrence.py` | Pure RRULE math (#471, ADR-0082): `validate_rrule` (tool-boundary check) + `next_due` (the next occurrence, **skip-missed** policy), date-only (naive) parsing. |
| `db.py` | `TaskStore` — SQLAlchemy ORM + CRUD helpers (list/add/complete/update/get/delete) for the local store (incl. the `repeat` column); `RepeatStore` — the `task_repeats` side table for external-provider recurrence rules (#471). |
| `service.py` | MCP tools (`tasks_list`/`tasks_lists`/`tasks_create_list`/`tasks_add`/`tasks_complete`/`tasks_update`/`tasks_delete`, the last two taking `repeat`) + manifest UI (+ `collections` spec) + the Tasks `board` page (one `PageSpec` + the pure `build_tasks_board` / `build_tasks_backlog` builders partitioning dated from undated (#766, folded onto Show in #820), view controls — group-by/scope plus the `backlog` Show value and its Calendar-view interplay — the **New list** board action, the backlog card's **Schedule** action, the `repeat` form field + badge, and `coerce_group`/`coerce_scope`/`coerce_show`, ADR-0049) + entity-reference, hover-card & chat-attachment helpers + `tasks_accounts` (the `/accounts` view). |
| `app.py` | Lifespan, provider router wiring, `GET /status`, `GET /accounts`, `GET /pages/{id}` (the one `board` page; `?show=backlog` for the undated partition — `can` 404s), `GET /attachments[/{ref_id}]`, `GET /resolve/{kind}/{ref_id}`, app factory. |
| `settings.py` | `TasksSettings` (adds `platform_url`, `database_url`). |
