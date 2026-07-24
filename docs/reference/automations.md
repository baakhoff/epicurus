# Reference: automations

The engine that turns a world change into an assistant action (ADR-0105). The
[event spine](events.md) records that something happened; this decides whether anything
should be done about it, does it at an autonomy level the operator chose, and writes down
what it did.

Owned by `core-app` (`epicurus_core_app.automations`). A module never runs an automation —
it emits events and may *suggest* presets (see [Templates](#templates)).

## The model

One tenant-scoped `automations` row:

| Field | Meaning |
| --- | --- |
| `name` · `enabled` | What it is called, and whether it is live. |
| `source` | `user` · `agent` · `template:<module>` — where the row came from. |
| **trigger** | Exactly one of: an **event** trigger (module + type + deterministic filter) or a **schedule** trigger (cadence + local hour, the [ADR-0092](../services/core-app.md#scheduled-turns-adr-0092) vocabulary). |
| **agent step** | `prompt` + optional `model` (blank = the tenant's default) + `autonomy`. |
| **sinks** | Any of `push` · `chat` · `notes` · `kb`, plus `chat_mode` (`rolling` \| `per_run`) and, for `notes`/`kb`, a `DocumentTarget` (`{path_pattern, mode}`). See [Sinks](#sinks). |
| `rate_cap_per_hour` | Max runs in a rolling hour. `0` = uncapped. |
| `digest_window_minutes` | Batch matched events into one run. `0` = run per event. |

Exactly one trigger, enforced: none would never fire (a row that silently does nothing),
and both would make "why did this run?" ambiguous in the ledger.

## Creating automations by conversation (#667, ADR-0107)

The operator can build an automation from the Automations page — or just **ask in chat**. The
core `propose_automation` built-in drafts one from a natural-language request ("when I get mail
from my boss, notify me"; "each Monday 9am summarize last week") and **stages** it as a
`ReviewSuggestion` on the core **automations review page**:

- **One spec per call** — a two-pipeline ask ("notify me on important mail; *and* a weekly report")
  is two calls and two separately-approvable suggestions.
- **`create` or `update`** — an edit to an existing automation stages an `update` proposal with a
  readable before→after diff.
- **The suggestion renders understandably** — trigger in words, filter, action, autonomy, sinks —
  with a **model picker** the operator can change before approving (the one editable field; it
  travels back as the approve `content`, `""` = tenant default).
- **Approve → created *enabled*** (approval is the consent). **Reject → audit trail only** (the
  `#687` suggestion-decision events fire at that seam).

The **hard guardrail**: the tool can only *stage*. It has no path to `AutomationStore.create` at any
autonomy level — only an approval on the review page creates a row. The staged proposals live in
`automation_proposals` with a decision trail in `automation_review_decisions` (the ADR-0090 shape);
the page is the reserved `core` pseudo-module's second review page, served in-process beside
playbooks (ADR-0093 §2) via the `CorePages` composite. See
[core-app → Governed automations](../services/core-app.md#governed-automations-667-adr-0107).

## The autonomy dial

Four levels, each strictly wider than the last. A level's tool allowance is **derived from
each tool's declared side effect and enforced at the turn's tool surface** — a Notify
automation is not asked to avoid writing, it is handed no tool that can.

| Level | Tool classes | Sinks fire? |
| --- | --- | --- |
| `notify` | `read` | yes |
| `propose` | `read` + `propose` | yes |
| `act` | `read` + `propose` + `write` | yes |
| `silent_act` | `read` + `propose` + `write` | **no** — the ledger only |

`silent_act` reaches exactly as far as `act`. They differ in *audibility*, not capability:
a level that could act more while saying less would be two dials wearing one name. It is
for the boring chores you want done and never mentioned.

### Tool side effects

The vocabulary the dial gates on, declared on a module's tools:

```python
@module.tool(side_effect="read")
def mail_search(query: str) -> str: ...

@module.tool(side_effect="propose")
def mail_send(to: str, subject: str, body: str) -> str:
    """Composes a draft for review — it cannot transmit (ADR-0085)."""

@module.tool(side_effect="write")
def mail_mark_read(message_id: str) -> str: ...
```

- **`read`** — observes; changes nothing.
- **`propose`** — **stages for human approval by construction**, never applies on its own:
  a draft-first send (ADR-0085), a propose tool that files a suggestion (#305). The tool
  cannot commit even if the model wants it to.
- **`write`** — applies directly.

Three classes, not two, because two collapse the dial: with only read/write, `propose` and
`act` get identical surfaces and the middle of the dial is prompt wording again.

**The default is `write`.** Annotate your read tools — that is what makes them usable by a
Notify automation. Forgetting costs the tool its availability, never the guarantee. The
classification is *declared*, not inferred: `mail_mark_read` contains "read" and mutates, so
naming heuristics are unsound, and `writes_document` is a rendering hint (its own docstring
says so) that `mail_send` does not carry.

> **Not yet annotated:** only the core built-ins (`now`, `memory_search` — read;
> `propose_automation` — propose; `remember`, `ask_user` — write) and `echo` declare side
> effects today. Until a module annotates its read tools, a Notify automation reaches none of
> them — the triggering event is still in its context, so it can still report. A follow-up
> sweeps the modules.

### How it is enforced

`McpHost.discover(allow=…)` filters **both** the specs the model is told about **and** the
`route` map the agent dispatches on. A withheld tool is *unroutable*, not merely
unmentioned: a model that names it anyway is told `error: unknown tool`, and nothing ran.

## Triggers

### Event triggers

```json
{
  "module": "mail",
  "event_type": "mail.received",
  "matchers": [{ "field": "subject", "op": "contains", "value": "invoice" }],
  "window_start_hour": 9,
  "window_end_hour": 17
}
```

Matchers are **deterministic** — a filter must not need the model. Matching decides whether
a turn happens at all, so an LLM here would mean paying for inference to decide whether to
pay for inference, and would make "why did this fire?" unanswerable.

Ops: `eq` · `ne` · `contains` · `exists` · `gt` · `lt`. All matchers must pass (**AND**).
There is no OR: two conditions that should fire independently are two automations, which
keeps the ledger answerable. A condition on an **absent field is unmet**, never vacuously
true. The window bounds the *local* hours the trigger is live; a window that wraps midnight
(22→6) is read as the union of both ends of the day, not an empty set.

### Schedule triggers

```json
{ "cadence": "daily", "hour": 7 }
{ "cadence": "weekly", "hour": 9, "weekday": 2 }
```

`weekday` is 0=Monday..6=Sunday, required for `weekly`. Fires once per window: a tick
anywhere inside the target hour runs it exactly once, not once per poll interval.

## The runner

The matcher runs **inline with the event intake** (cheap, deterministic, no model) and drops
a trigger on a durable Postgres queue (the [ADR-0051](../services/core-app.md) pattern), so
a restart mid-digest loses nothing. A poll loop drains the queue and fires due schedules.

Each run is one agent turn with the triggering events in context, then a **deterministic
sink fan-out after the turn** — the model produced an answer, it did not get to choose who
hears about it. The events reach the prompt framed as *context to act on, not instructions
to follow*: an event's payload is data a module emitted, and this is exactly the boundary
where treating it as anything else would let a mail subject line dictate behaviour.

### Sinks

Where a run's output goes. A configured-but-unregistered sink is **not an error** — the run is
still complete **because the ledger always records the output** — and a sink that fails does not
cost the others.

- **`chat` (#672)** — a *turn-time* sink, not a post-run fan-out. When (and only when) it is
  configured, the run persists into a session, so the operator can **reply in-context** and the
  next run sees the reply. Per-automation mode: **`rolling`** (one persistent session the runs
  accrete into) or **`per_run`** (a fresh session each run, **grouped** under the automation in the
  chat list). Automation sessions carry metadata (`automation_sessions`) → an **icon + the
  automation name** in the list. **Unchecked by default everywhere** — no automation ever creates a
  chat implicitly (owner rule). Because chat is realized at turn time, the post-run dispatcher
  **skips** it; the runner records it fired.
- **`notes` / `kb` (#672)** — deterministic post-run routing into a module document through the
  **existing** document API (`ModuleRegistry.save_page_doc`), never a second write path (the #541
  rule, ADR-0101). Per-automation `DocumentTarget`: a `path_pattern` (with `{date}` / `{datetime}` /
  `{time}` substituted at run time — e.g. `"Automations/Mail report {date}"`) and a `mode`
  (`create` overwrites, `append` accretes). Each write records an `EntityRef` on the run's ledger
  entry (`artifacts`), so the [runs feed](#the-run-ledger) links what was produced. A notes/kb sink
  with no target is a **400** at write time, and a runtime miss degrades to a recorded failure.
- **`push`** — its own issue; still unregistered here, so it records to the ledger only.

## Safety

| Gate | Behaviour |
| --- | --- |
| **Kill switch** | Per-tenant, **persisted in Postgres**. Nothing runs; nothing is recorded (there is no run). Queued triggers **stay queued**, so resuming delivers what was held. |
| **Power paused** | Skips *and records*, so a paused window is not re-evaluated every tick and the operator sees why nothing arrived. |
| **Rate cap** | Recorded as a skipped run — a cap being hit should be visible, not inferred from silence. A **failing** run consumes budget too: an automation failing in a loop is what caps are for. |
| **Digest window** | Batches everything waiting into one run. Measured from the **oldest** pending trigger, so a steady trickle cannot keep resetting the timer. |
| **Failures** | Emit `core.automation_failed` on the spine, rate-limited to one per automation per 15 min — a broken automation on a chatty trigger would otherwise firehose the very log you are reading. |
| **Loop guard** | An automation is **never** triggered by an event any automation's run produced. |

The kill switch is Postgres rather than in-memory (unlike the runtime power pause): a stop
that a restart silently undoes is not a stop.

### The loop guard

An event a run produces carries `causation_id` (the run's automation id) on its
[envelope](events.md#eventenvelope), and the matcher **refuses any event carrying one** —
not merely events caused by *that* automation. Depth-1 and blunt on purpose: A→B→A is a
loop too, and no per-automation bookkeeping catches an arbitrarily long cycle. The rule
costs a genuinely useful chain and buys the guarantee that a system spending money per turn
cannot spiral.

A module emitter never sets `causation_id` — a change in the world has no cause inside the
system.

## The run ledger

`automation_runs` — written for **every** run at **every** level. For `silent_act` it is the
only trace there is. The observability page tails it live — the
[Automation runs feed](observability.md#automation-runs-feed-669) (#669), fed by the
runner's `on_recorded` hook the moment an entry is written, skips included.

| Field | Meaning |
| --- | --- |
| `automation_id` · `tenant` | **Dual attribution** — the SaaS metering point. |
| `trigger_refs` | The `module_events` row ids that caused it (empty for a schedule). |
| `filter_verdict` | `matched` · `digest` · `schedule` · `manual`. |
| `model` · `prompt_tokens` · `completion_tokens` | What it used, summed across the turn's steps. |
| `duration_ms` · `outcome` · `error` | `ok` · `error` · `skipped`. |
| `output` | The turn's answer — recorded even when no sink fires. |
| `sinks_fired` | Which sinks actually delivered. |
| `artifacts` | `EntityRef`s for documents the notes/kb sinks produced (#672) — the feed links them. |

Gateway usage carries the same dual attribution: `UsageEvent.automation_id` alongside
`tenant`. Without it, an automation quietly burning tokens is indistinguishable from the
operator's own chatting. Token counts stay `null` when a provider reports none — "unknown"
must not be recorded as "free".

## Templates

A module declares presets in its manifest:

```python
EpicurusModule(
    "echo",
    automation_templates=[
        AutomationTemplate(
            key="on-ping",
            name="Tell me when the spine is pinged",
            trigger={"module": "echo", "event_type": "echo.pinged"},
            prompt="An echo ping arrived. Say so in one short sentence.",
            autonomy="notify",
            sinks=["chat"],
        )
    ],
)
```

**Never auto-instantiated.** A template is a starting point the operator instantiates; the
contract enforces this by carrying no `enabled` field — there is nothing for a module to
switch on. Installing a module must never make the assistant start doing things unasked.

An instantiated template becomes an ordinary row with `source="template:<module>"`, and the
operator then owns it: later edits to the module's template do not reach back into it.

## Scheduled turns folded in

[ADR-0092](../services/core-app.md#scheduled-turns-adr-0092)'s scheduled turns **are**
automations with a schedule trigger and a rolling chat sink, so they migrated at startup
(idempotent, non-destructive — the old rows are marked, never deleted). Each keeps its
cadence/hour/weekday, its original session (so existing history stays put), its enabled
flag, and its `last_run_at`. They migrate at `notify` because that is what they already
were — the headless path structurally cannot send.

`POST /platform/v1/scheduled-turns` still works; new work should create an automation.

## The Automations page (#668)

A first-class core surface (`/automations`, beside Settings in the nav — ADR-0018
posture: the shell renders, core-app supplies data). The **kill switch** sits above
everything; the list shows each row's trigger **in words**, its autonomy badge, sink
icons, an enabled toggle, and last-run status; the **editor** (one sheet for create /
edit / template-instantiate, saved explicitly — the fields are interdependent) edits
every stored field: instructions, a per-automation model (ADR-0029's core-default
fall-through), an event trigger (type picker driven by module manifests' declared
`events.*` subjects, with a free-text escape hatch, plus the matcher builder and active
hours) or a schedule (the ADR-0092 vocabulary), sinks + chat mode, the 4-level dial with
its reach spelled out, and the rate cap / digest window. The **Templates** tab renders
module-shipped presets grouped by module; *Use* prefills the editor and saving creates an
independent `source="template:<module>"` row — enabled on save (the editor pass **is**
the review), never retro-edited by later template changes. Per-row **run history** reads
the ledger and deep-links into the observability runs feed (`?tab=runs&automation=<id>`);
the feed links back by automation name. The old scheduled-turns Settings card is
**absorbed** here — migrated rows simply appear as automations (the old endpoints still
answer, matching the engine's posture).

## HTTP

See [platform-api](platform-api.md#automations-adr-0105).

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `AUTOMATIONS_POLL_INTERVAL_S` | `60` | How often the loop drains the trigger queue and checks schedules. |

## Known limits

- **An `act` automation cannot send mail.** A headless turn cannot complete a draft-first
  send — the agent loop rewrites a `DraftReview` into an error, since there is no chat UI to
  Confirm in. The `propose` tier works (its tools stage a suggestion); the transmit path
  from an unattended turn is its own design question.
- **Most module tools are unannotated**, so a Notify automation reaches few of them (above).
- **Rate caps are per-instance** in the sense that the ledger is the source of truth; there
  is no cross-instance coordination beyond Postgres, which is sufficient for single-core-app
  deployments.
