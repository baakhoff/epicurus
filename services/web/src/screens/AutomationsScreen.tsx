/**
 * The Automations page (#668) — a first-class core surface (ADR-0018 posture: the shell
 * renders, the core supplies data), like Settings or Observability.
 *
 * Two tabs: **Automations** (the operator's rows — list, editor, per-row run history) and
 * **Templates** (module-shipped presets, grouped by module — never auto-instantiated;
 * "Use" prefills the editor and saving creates a real, independent automation). The
 * tenant-wide **kill switch** sits above both: a stop must be visible wherever you are.
 *
 * The editor is one Sheet shared by create / edit / template-instantiate, holding a local
 * draft saved **explicitly** — the fields are interdependent (exactly one trigger; weekly
 * needs a weekday), so per-field auto-save would persist invalid intermediate states
 * (the ADR-0098 rationale). Each open mounts fresh state from props (the Sheet unmounts
 * when closed — the CommandPalette convention), so there are no reset effects.
 *
 * Vocabularies (autonomy levels, sinks, matcher ops) come from the engine's
 * `/vocabulary` endpoint, and the event-type picker is driven by the live event catalog —
 * module manifests' declared `events.*` subjects — with a free-text escape hatch for a
 * type no manifest declares (the core's own `files.*` / `core.*` families, or a module
 * not yet installed).
 */
import {
  Bell,
  BookOpen,
  CalendarClock,
  ChevronDown,
  ChevronUp,
  MessageSquare,
  Play,
  Plus,
  StickyNote,
  Trash2,
  Zap,
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  Badge,
  Button,
  Card,
  Confirm,
  EmptyState,
  Label,
  NumberInput,
  Select,
  Sheet,
  Spinner,
  Switch,
  Tabs,
  TextArea,
  TextInput,
  cn,
} from "@/components/ui";
import type { TabSpec } from "@/components/ui";
import { api } from "@/lib/api";
import type {
  Automation,
  AutomationDraft,
  AutomationMatcher,
  AutomationRun,
  AutomationTemplate,
} from "@/lib/contracts";

/* ── vocabulary helpers ──────────────────────────────────────────────────── */

const WEEKDAY_LABELS = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
];

/** What each dial level lets a run's turns reach — the ADR-0105 ladder, for the editor's
 * hint text (the authoritative allowance is server-derived on each row). */
const AUTONOMY_HINTS: Record<string, string> = {
  notify: "Read-only tools; output goes to the sinks.",
  propose: "May stage suggestions for your review; never applies them itself.",
  act: "May use write tools directly; the run report goes to the sinks.",
  silent_act: "Acts like Act, but reports only to the run ledger — no sinks fire.",
};

const SINK_ICONS: Record<string, typeof MessageSquare> = {
  chat: MessageSquare,
  notes: StickyNote,
  kb: BookOpen,
  push: Bell,
};

function formatHour(hour: number): string {
  return `${String(hour).padStart(2, "0")}:00`;
}

/** The list's trigger summary, in words — the row must be readable without the editor. */
export function triggerSummary(a: Automation): string {
  if (a.schedule_trigger) {
    const s = a.schedule_trigger;
    if (s.cadence === "weekly") {
      const day = WEEKDAY_LABELS[s.weekday ?? 0] ?? `day ${s.weekday}`;
      return `Weekly on ${day} at ${formatHour(s.hour)}`;
    }
    return `Daily at ${formatHour(s.hour)}`;
  }
  if (a.event_trigger) {
    const t = a.event_trigger;
    const parts = [`When ${t.event_type} arrives`];
    if (t.matchers.length > 0) {
      parts.push(
        "matching " +
          t.matchers
            .map((m) =>
              m.op === "exists" ? `${m.field} exists` : `${m.field} ${m.op} ${String(m.value)}`,
            )
            .join(" and "),
      );
    }
    if (t.window_start_hour != null && t.window_end_hour != null) {
      parts.push(`between ${formatHour(t.window_start_hour)}–${formatHour(t.window_end_hour)}`);
    }
    return parts.join(", ");
  }
  return "Never fires (no trigger)";
}

function shortWhen(iso: string | null | undefined): string {
  if (!iso) return "never";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  } catch {
    return iso;
  }
}

/* ── the kill switch ─────────────────────────────────────────────────────── */

function KillSwitchCard() {
  const queryClient = useQueryClient();
  const kill = useQuery({ queryKey: ["automation-kill"], queryFn: api.automationKillSwitch });
  const set = useMutation({
    mutationFn: (halted: boolean) => api.setAutomationKillSwitch(halted),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["automation-kill"] }),
  });
  const halted = kill.data?.halted ?? false;

  return (
    <Card
      className={cn(
        "flex items-center justify-between gap-3 p-4",
        halted && "border-danger/50",
      )}
    >
      <div className="flex flex-col gap-0.5">
        <span className={cn("text-sm", halted ? "text-danger" : "text-ink")}>
          {halted ? "All automations are stopped" : "Automations are running"}
        </span>
        <span className="text-xs text-ink-faint">
          The kill switch halts every automation for this tenant. It survives a restart;
          queued triggers stay queued and deliver when you resume.
        </span>
      </div>
      <Switch
        checked={!halted}
        onChange={(next) => set.mutate(!next)}
        label="Automations kill switch"
        disabled={kill.isLoading || set.isPending}
      />
    </Card>
  );
}

/* ── per-automation run history ──────────────────────────────────────────── */

function RunHistory({ automation }: { automation: Automation }) {
  const runs = useQuery({
    queryKey: ["automation-runs", automation.id],
    queryFn: () => api.automationRuns({ automationId: automation.id, limit: 20 }),
  });

  if (runs.isLoading) return <Spinner className="size-4" />;
  const entries: AutomationRun[] = runs.data ?? [];
  return (
    <div className="flex flex-col gap-1">
      {entries.length === 0 ? (
        <span className="text-xs text-ink-faint">No runs yet.</span>
      ) : (
        <ul className="flex flex-col divide-y divide-edge">
          {entries.map((run) => (
            <li key={run.id} className="flex flex-wrap items-baseline gap-2 py-1 text-[11px]">
              <span className="shrink-0 font-mono text-ink-faint">
                {shortWhen(run.started_at)}
              </span>
              <span className="shrink-0 text-ink-dim">{run.filter_verdict}</span>
              <Badge
                tone={
                  run.outcome === "ok" ? "ok" : run.outcome === "skipped" ? "warn" : "danger"
                }
                className="shrink-0 font-mono uppercase"
              >
                {run.outcome}
              </Badge>
              {run.error && <span className="break-all text-warn">{run.error}</span>}
              {run.duration_ms != null && (
                <span className="shrink-0 text-ink-faint">{run.duration_ms} ms</span>
              )}
            </li>
          ))}
        </ul>
      )}
      <Link
        to={`/observability?tab=runs&automation=${encodeURIComponent(automation.id)}`}
        className="self-start text-xs text-accent-strong hover:underline"
      >
        Open in Observability →
      </Link>
    </div>
  );
}

/* ── one automation row ──────────────────────────────────────────────────── */

function AutomationRow({
  automation,
  onEdit,
}: {
  automation: Automation;
  onEdit: (a: Automation) => void;
}) {
  const queryClient = useQueryClient();
  const [historyOpen, setHistoryOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["automations"] });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.setAutomationEnabled(automation.id, enabled),
    onSuccess: invalidate,
  });
  const remove = useMutation({ mutationFn: () => api.deleteAutomation(automation.id), onSuccess: invalidate });
  const runNow = useMutation({
    mutationFn: () => api.runAutomationNow(automation.id),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["automation-runs", automation.id] }),
  });

  return (
    <li className="flex flex-col gap-2 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm text-ink">{automation.name}</span>
        <Badge tone={automation.autonomy === "notify" ? "dim" : "accent"} className="font-mono">
          {automation.autonomy}
        </Badge>
        {automation.source.startsWith("template:") && (
          <Badge tone="dim" className="font-mono">
            {automation.source}
          </Badge>
        )}
        <span className="flex items-center gap-1 text-ink-dim">
          {automation.sinks.map((sink) => {
            const Icon = SINK_ICONS[sink] ?? Zap;
            return <Icon key={sink} size={13} aria-label={`sink: ${sink}`} />;
          })}
        </span>
        <span className="ml-auto">
          <Switch
            checked={automation.enabled}
            onChange={(next) => toggle.mutate(next)}
            label={`${automation.name} enabled`}
            disabled={toggle.isPending}
          />
        </span>
      </div>
      <span className="text-xs text-ink-dim">{triggerSummary(automation)}</span>
      <div className="flex flex-wrap items-center gap-2 text-xs text-ink-faint">
        <span>
          Last run: {shortWhen(automation.last_run_at)}
          {automation.last_status ? ` — ${automation.last_status}` : ""}
        </span>
        <span className="ml-auto flex items-center gap-1">
          <Button variant="ghost" className="text-xs" onClick={() => onEdit(automation)}>
            Edit
          </Button>
          <Button
            variant="ghost"
            className="text-xs"
            onClick={() => runNow.mutate()}
            disabled={runNow.isPending}
            aria-label={`Run ${automation.name} now`}
          >
            <Play size={12} /> Run now
          </Button>
          <Button
            variant="ghost"
            className="text-xs"
            onClick={() => setHistoryOpen((v) => !v)}
            aria-expanded={historyOpen}
          >
            {historyOpen ? <ChevronUp size={12} /> : <ChevronDown size={12} />} History
          </Button>
          <Button
            variant="ghost"
            className="text-xs text-danger"
            onClick={() => setConfirmDelete(true)}
            aria-label={`Delete ${automation.name}`}
          >
            <Trash2 size={12} />
          </Button>
        </span>
      </div>
      {runNow.isError && (
        <span className="text-xs text-danger">
          Run refused: {runNow.error instanceof Error ? runNow.error.message : "error"}
        </span>
      )}
      {historyOpen && <RunHistory automation={automation} />}
      <Confirm
        open={confirmDelete}
        message={`Delete "${automation.name}"? Its run history stays in the ledger.`}
        confirmLabel="Delete"
        danger
        onConfirm={() => {
          setConfirmDelete(false);
          remove.mutate();
        }}
        onCancel={() => setConfirmDelete(false)}
      />
    </li>
  );
}

/* ── the editor (create / edit / instantiate-template) ───────────────────── */

type EditorSeed =
  | { kind: "new" }
  | { kind: "edit"; automation: Automation }
  | { kind: "template"; template: AutomationTemplate };

function draftFrom(seed: EditorSeed): AutomationDraft {
  if (seed.kind === "edit") {
    const a = seed.automation;
    return {
      name: a.name,
      prompt: a.prompt,
      autonomy: a.autonomy,
      event_trigger: a.event_trigger ?? null,
      schedule_trigger: a.schedule_trigger ?? null,
      model: a.model ?? null,
      sinks: [...a.sinks],
      chat_mode: a.chat_mode,
      rate_cap_per_hour: a.rate_cap_per_hour,
      digest_window_minutes: a.digest_window_minutes,
      enabled: a.enabled,
    };
  }
  if (seed.kind === "template") {
    const t = seed.template;
    const trigger = t.trigger as Record<string, unknown>;
    const isSchedule = typeof trigger.cadence === "string";
    return {
      name: t.name,
      prompt: t.prompt,
      autonomy: t.autonomy,
      event_trigger: isSchedule
        ? null
        : {
            module: String(trigger.module ?? ""),
            event_type: String(trigger.event_type ?? ""),
            matchers: [],
            window_start_hour: null,
            window_end_hour: null,
          },
      schedule_trigger: isSchedule
        ? {
            cadence: String(trigger.cadence),
            hour: Number(trigger.hour ?? 7),
            weekday: trigger.weekday == null ? null : Number(trigger.weekday),
          }
        : null,
      model: null,
      sinks: [...t.sinks],
      chat_mode: "rolling",
      rate_cap_per_hour: 0,
      digest_window_minutes: 0,
      enabled: true,
    };
  }
  return {
    name: "",
    prompt: "",
    autonomy: "notify",
    event_trigger: null,
    schedule_trigger: { cadence: "daily", hour: 7, weekday: null },
    model: null,
    sinks: ["chat"],
    chat_mode: "rolling",
    rate_cap_per_hour: 0,
    digest_window_minutes: 0,
    enabled: true,
  };
}

function AutomationEditor({ seed, onClose }: { seed: EditorSeed; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<AutomationDraft>(() => draftFrom(seed));
  const [error, setError] = useState<string | null>(null);

  const vocabulary = useQuery({
    queryKey: ["automation-vocabulary"],
    queryFn: api.automationVocabulary,
  });
  const modules = useQuery({ queryKey: ["modules"], queryFn: () => api.modules() });
  const localModels = useQuery({ queryKey: ["models"], queryFn: () => api.models() });
  const hostedModels = useQuery({ queryKey: ["saved-models"], queryFn: api.savedModels });

  // The live event catalog: every `events.*` subject the installed modules declare,
  // grouped by module. The core's own emitters (files.*, core.*) have no manifest, so a
  // free-text "custom" entry stays available.
  const catalog = new Map<string, string[]>();
  for (const snapshot of modules.data ?? []) {
    const types = snapshot.manifest.events_emitted
      .map((e) => e.subject)
      .filter((s) => s.startsWith("events."))
      .map((s) => s.slice("events.".length));
    if (types.length) catalog.set(snapshot.manifest.name, types);
  }
  const declaredTypes = [...catalog.values()].flat().sort();

  const save = useMutation({
    mutationFn: async () => {
      if (seed.kind === "edit") {
        return api.updateAutomation(seed.automation.id, draft);
      }
      return api.createAutomation(
        seed.kind === "template" ? { ...draft, source: `template:${seed.template.module}` } : draft,
      );
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["automations"] });
      onClose();
    },
    onError: (err: unknown) => setError(err instanceof Error ? err.message : "save failed"),
  });

  const patch = (part: Partial<AutomationDraft>) => setDraft((d) => ({ ...d, ...part }));
  const triggerKind = draft.event_trigger ? "event" : "schedule";
  const eventTrigger = draft.event_trigger;
  const scheduleTrigger = draft.schedule_trigger;
  const usesChat = draft.sinks.includes("chat");
  const knownType =
    eventTrigger != null &&
    (eventTrigger.event_type === "" || declaredTypes.includes(eventTrigger.event_type));

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1.5">
        <Label>Name</Label>
        <TextInput
          id="automation-name"
          aria-label="Name"
          value={draft.name}
          onChange={(e) => patch({ name: e.target.value })}
          placeholder="Tell me about invoices"
        />
      </div>

      <div className="flex flex-col gap-1.5">
        <Label>Instructions</Label>
        <TextArea
          id="automation-prompt"
          aria-label="Instructions"
          value={draft.prompt}
          onChange={(e) => patch({ prompt: e.target.value })}
          rows={4}
          placeholder="What should the assistant do when this fires?"
        />
      </div>

      {/* ── trigger ── */}
      <div className="flex flex-col gap-1.5">
        <Label>Trigger</Label>
        <Select
          id="automation-trigger-kind"
          value={triggerKind}
          onChange={(e) => {
            if (e.target.value === "event") {
              patch({
                event_trigger: {
                  module: "",
                  event_type: "",
                  matchers: [],
                  window_start_hour: null,
                  window_end_hour: null,
                },
                schedule_trigger: null,
              });
            } else {
              patch({
                event_trigger: null,
                schedule_trigger: { cadence: "daily", hour: 7, weekday: null },
              });
            }
          }}
        >
          <option value="event">When an event arrives</option>
          <option value="schedule">On a schedule</option>
        </Select>
      </div>

      {eventTrigger && (
        <div className="flex flex-col gap-3 rounded-(--radius-field) border border-edge p-3">
          <div className="flex flex-col gap-1.5">
            <Label>Event type</Label>
            <Select
              id="automation-event-type"
              value={knownType ? eventTrigger.event_type : "__custom__"}
              onChange={(e) => {
                const value = e.target.value;
                if (value === "__custom__") {
                  patch({
                    event_trigger: { ...eventTrigger, event_type: "custom.event", module: "" },
                  });
                  return;
                }
                patch({
                  event_trigger: {
                    ...eventTrigger,
                    event_type: value,
                    module: value.split(".")[0] ?? "",
                  },
                });
              }}
            >
              <option value="">Pick an event…</option>
              {[...catalog.entries()].map(([module, types]) => (
                <optgroup key={module} label={module}>
                  {types.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </optgroup>
              ))}
              <option value="__custom__">Custom type…</option>
            </Select>
            {!knownType && (
              <TextInput
                value={eventTrigger.event_type}
                onChange={(e) =>
                  patch({
                    event_trigger: {
                      ...eventTrigger,
                      event_type: e.target.value,
                      module: e.target.value.split(".")[0] ?? "",
                    },
                  })
                }
                placeholder="files.file_added"
                aria-label="Custom event type"
              />
            )}
          </div>

          <div className="flex flex-col gap-1.5">
            <Label>Only when (all must match)</Label>
            {eventTrigger.matchers.map((matcher, i) => (
              <div key={i} className="flex flex-wrap items-center gap-1.5">
                <TextInput
                  value={matcher.field}
                  onChange={(e) => {
                    const matchers = [...eventTrigger.matchers];
                    matchers[i] = { ...matcher, field: e.target.value };
                    patch({ event_trigger: { ...eventTrigger, matchers } });
                  }}
                  placeholder="subject"
                  className="w-28"
                  aria-label={`Matcher ${i + 1} field`}
                />
                <Select
                  size="sm"
                  value={matcher.op}
                  onChange={(e) => {
                    const matchers = [...eventTrigger.matchers];
                    matchers[i] = { ...matcher, op: e.target.value as AutomationMatcher["op"] };
                    patch({ event_trigger: { ...eventTrigger, matchers } });
                  }}
                  aria-label={`Matcher ${i + 1} operator`}
                >
                  {(vocabulary.data?.matcher_ops ?? ["eq", "contains", "exists"]).map((op) => (
                    <option key={op} value={op}>
                      {op}
                    </option>
                  ))}
                </Select>
                {matcher.op !== "exists" && (
                  <TextInput
                    value={String(matcher.value ?? "")}
                    onChange={(e) => {
                      const matchers = [...eventTrigger.matchers];
                      matchers[i] = { ...matcher, value: e.target.value };
                      patch({ event_trigger: { ...eventTrigger, matchers } });
                    }}
                    placeholder="invoice"
                    className="w-28"
                    aria-label={`Matcher ${i + 1} value`}
                  />
                )}
                <Button
                  variant="ghost"
                  className="text-xs"
                  aria-label={`Remove matcher ${i + 1}`}
                  onClick={() =>
                    patch({
                      event_trigger: {
                        ...eventTrigger,
                        matchers: eventTrigger.matchers.filter((_, j) => j !== i),
                      },
                    })
                  }
                >
                  <Trash2 size={12} />
                </Button>
              </div>
            ))}
            <Button
              variant="ghost"
              className="self-start text-xs"
              onClick={() =>
                patch({
                  event_trigger: {
                    ...eventTrigger,
                    matchers: [...eventTrigger.matchers, { field: "", op: "contains", value: "" }],
                  },
                })
              }
            >
              <Plus size={12} /> Add condition
            </Button>
          </div>

          <div className="flex items-center gap-2">
            <Label>Active hours</Label>
            <Select
              id="automation-window-start"
              size="sm"
              value={eventTrigger.window_start_hour == null ? "" : String(eventTrigger.window_start_hour)}
              onChange={(e) =>
                patch({
                  event_trigger: {
                    ...eventTrigger,
                    window_start_hour: e.target.value === "" ? null : Number(e.target.value),
                    window_end_hour:
                      e.target.value === "" ? null : (eventTrigger.window_end_hour ?? 17),
                  },
                })
              }
              aria-label="Window start hour"
            >
              <option value="">Always</option>
              {Array.from({ length: 24 }, (_, h) => (
                <option key={h} value={h}>
                  {formatHour(h)}
                </option>
              ))}
            </Select>
            {eventTrigger.window_start_hour != null && (
              <>
                <span className="text-xs text-ink-faint">to</span>
                <Select
                  size="sm"
                  value={String(eventTrigger.window_end_hour ?? 17)}
                  onChange={(e) =>
                    patch({
                      event_trigger: {
                        ...eventTrigger,
                        window_end_hour: Number(e.target.value),
                      },
                    })
                  }
                  aria-label="Window end hour"
                >
                  {Array.from({ length: 24 }, (_, h) => (
                    <option key={h} value={h}>
                      {formatHour(h)}
                    </option>
                  ))}
                </Select>
              </>
            )}
          </div>
        </div>
      )}

      {scheduleTrigger && (
        <div className="flex flex-wrap items-center gap-2 rounded-(--radius-field) border border-edge p-3">
          <CalendarClock size={14} className="text-ink-dim" />
          <Select
            size="sm"
            value={scheduleTrigger.cadence}
            onChange={(e) =>
              patch({
                schedule_trigger: {
                  ...scheduleTrigger,
                  cadence: e.target.value,
                  weekday: e.target.value === "weekly" ? (scheduleTrigger.weekday ?? 0) : null,
                },
              })
            }
            aria-label="Cadence"
          >
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
          </Select>
          {scheduleTrigger.cadence === "weekly" && (
            <Select
              size="sm"
              value={String(scheduleTrigger.weekday ?? 0)}
              onChange={(e) =>
                patch({
                  schedule_trigger: { ...scheduleTrigger, weekday: Number(e.target.value) },
                })
              }
              aria-label="Weekday"
            >
              {WEEKDAY_LABELS.map((label, i) => (
                <option key={label} value={i}>
                  {label}
                </option>
              ))}
            </Select>
          )}
          <span className="text-xs text-ink-faint">at</span>
          <Select
            size="sm"
            value={String(scheduleTrigger.hour)}
            onChange={(e) =>
              patch({ schedule_trigger: { ...scheduleTrigger, hour: Number(e.target.value) } })
            }
            aria-label="Hour"
          >
            {Array.from({ length: 24 }, (_, h) => (
              <option key={h} value={h}>
                {formatHour(h)}
              </option>
            ))}
          </Select>
        </div>
      )}

      {/* ── model ── */}
      <div className="flex flex-col gap-1.5">
        <Label>Model</Label>
        <Select
          id="automation-model"
          value={draft.model ?? ""}
          onChange={(e) => patch({ model: e.target.value || null })}
        >
          <option value="">Core default</option>
          {(localModels.data ?? [])
            .filter((m) => !m.hidden)
            .map((m) => (
              <option key={m.name} value={m.name}>
                {m.name}
              </option>
            ))}
          {(hostedModels.data ?? []).length > 0 && (
            <optgroup label="Hosted">
              {(hostedModels.data ?? []).map((m) => (
                <option key={m.model} value={m.model}>
                  {m.model}
                </option>
              ))}
            </optgroup>
          )}
        </Select>
      </div>

      {/* ── autonomy dial ── */}
      <div className="flex flex-col gap-1.5">
        <Label>Autonomy</Label>
        <Select
          id="automation-autonomy"
          value={draft.autonomy}
          onChange={(e) => patch({ autonomy: e.target.value })}
        >
          {(vocabulary.data?.autonomy_levels ?? ["notify"]).map((level) => (
            <option key={level} value={level}>
              {level}
            </option>
          ))}
        </Select>
        <span className="text-xs text-ink-faint">
          {AUTONOMY_HINTS[draft.autonomy] ?? "Tool reach is enforced at the tool surface."}
        </span>
      </div>

      {/* ── sinks ── */}
      <div className="flex flex-col gap-1.5">
        <Label>Deliver to</Label>
        <div className="flex flex-wrap items-center gap-3">
          {(vocabulary.data?.sinks ?? ["chat"]).map((sink) => {
            const Icon = SINK_ICONS[sink] ?? Zap;
            const checked = draft.sinks.includes(sink);
            return (
              <label key={sink} className="flex items-center gap-1.5 text-sm text-ink">
                {/* eslint-disable-next-line no-restricted-syntax -- a checkbox: the #394
                    field kit has no checkbox primitive, and a Switch per sink reads as
                    four independent settings rather than one multi-select. */}
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={(e) =>
                    patch({
                      sinks: e.target.checked
                        ? [...draft.sinks, sink]
                        : draft.sinks.filter((s) => s !== sink),
                    })
                  }
                  aria-label={`sink ${sink}`}
                />
                <Icon size={13} className="text-ink-dim" />
                {sink}
              </label>
            );
          })}
        </div>
        {usesChat && (
          <div className="flex items-center gap-2">
            <Label>Chat mode</Label>
            <Select
              id="automation-chat-mode"
              size="sm"
              value={draft.chat_mode}
              onChange={(e) => patch({ chat_mode: e.target.value })}
            >
              <option value="rolling">One rolling conversation</option>
              <option value="per_run">A new conversation per run</option>
            </Select>
          </div>
        )}
      </div>

      {/* ── caps ── */}
      <div className="flex flex-wrap items-center gap-4">
        <div className="flex items-center gap-2">
          <Label>Rate cap /hour</Label>
          <NumberInput
            id="automation-rate-cap"
            value={draft.rate_cap_per_hour}
            min={0}
            onChange={(e) => patch({ rate_cap_per_hour: Number(e.target.value) || 0 })}
            className="w-20"
          />
        </div>
        <div className="flex items-center gap-2">
          <Label>Digest window (min)</Label>
          <NumberInput
            id="automation-digest"
            value={draft.digest_window_minutes}
            min={0}
            onChange={(e) => patch({ digest_window_minutes: Number(e.target.value) || 0 })}
            className="w-20"
          />
        </div>
      </div>
      <span className="text-xs text-ink-faint">
        0 = uncapped / run per event. A digest batches matched events into one run.
      </span>

      <div className="flex items-center justify-between gap-2">
        <label className="flex items-center gap-2 text-sm text-ink">
          <Switch
            checked={draft.enabled}
            onChange={(enabled) => patch({ enabled })}
            label="Enabled on save"
          />
          Enabled
        </label>
        <span className="flex items-center gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={() => save.mutate()} disabled={save.isPending}>
            {seed.kind === "edit" ? "Save changes" : "Create automation"}
          </Button>
        </span>
      </div>
      {error && <span className="text-xs text-danger">{error}</span>}
    </div>
  );
}

/* ── templates tab ───────────────────────────────────────────────────────── */

function TemplatesTab({ onUse }: { onUse: (t: AutomationTemplate) => void }) {
  const templates = useQuery({
    queryKey: ["automation-templates"],
    queryFn: api.automationTemplates,
  });

  if (templates.isLoading) return <Spinner className="size-4" />;
  const rows = templates.data ?? [];
  if (rows.length === 0) {
    return (
      <EmptyState>
        <span className="text-sm text-ink-dim">
          No templates yet — modules ship presets here as they adopt the spine. Nothing is
          ever instantiated without you.
        </span>
      </EmptyState>
    );
  }
  const byModule = new Map<string, AutomationTemplate[]>();
  for (const t of rows) {
    byModule.set(t.module, [...(byModule.get(t.module) ?? []), t]);
  }
  return (
    <div className="flex flex-col gap-4">
      {[...byModule.entries()].map(([module, list]) => (
        <Card key={module} className="flex flex-col gap-2 p-4">
          <h2 className="font-mono text-xs uppercase text-ink-faint">{module}</h2>
          <ul className="flex flex-col divide-y divide-edge">
            {list.map((t) => (
              <li key={t.key} className="flex flex-col gap-1 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm text-ink">{t.name}</span>
                  <Badge tone="dim" className="font-mono">
                    {t.autonomy}
                  </Badge>
                  <span className="ml-auto">
                    <Button
                      variant="ghost"
                      className="text-xs"
                      onClick={() => onUse(t)}
                      aria-label={`Use template ${t.name}`}
                    >
                      Use
                    </Button>
                  </span>
                </div>
                {t.description && <span className="text-xs text-ink-dim">{t.description}</span>}
              </li>
            ))}
          </ul>
        </Card>
      ))}
      <p className="text-xs text-ink-faint">
        Using a template prefills the editor; saving creates your own independent automation —
        later template changes never touch it.
      </p>
    </div>
  );
}

/* ── the page ────────────────────────────────────────────────────────────── */

type PageTab = "automations" | "templates";
const PAGE_TABS: TabSpec<PageTab>[] = [
  { id: "automations", label: "Automations" },
  { id: "templates", label: "Templates" },
];

export function AutomationsScreen() {
  const [tab, setTab] = useState<PageTab>("automations");
  const [editor, setEditor] = useState<EditorSeed | null>(null);
  const automations = useQuery({ queryKey: ["automations"], queryFn: api.automations });

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-2xl flex-col gap-4 px-4 py-5">
        <div className="flex items-center justify-between gap-3">
          <h1 className="font-serif text-xl text-ink">Automations</h1>
          <Tabs tabs={PAGE_TABS} value={tab} onChange={setTab} label="Automations views" />
        </div>

        <KillSwitchCard />

        {tab === "automations" ? (
          <Card className="flex flex-col gap-2 p-4">
            <div className="flex items-center justify-between">
              <span className="text-xs text-ink-faint">
                What runs on its own, and how far each may go.
              </span>
              <Button className="text-xs" onClick={() => setEditor({ kind: "new" })}>
                <Plus size={12} /> New automation
              </Button>
            </div>
            {automations.isLoading ? (
              <Spinner className="size-4" />
            ) : (automations.data ?? []).length === 0 ? (
              <EmptyState>
                <span className="text-sm text-ink-dim">
                  No automations yet — create one, or start from a module's template.
                  Scheduled turns you had before already migrated here.
                </span>
              </EmptyState>
            ) : (
              <ul className="flex flex-col divide-y divide-edge">
                {(automations.data ?? []).map((a) => (
                  <AutomationRow
                    key={a.id}
                    automation={a}
                    onEdit={(automation) => setEditor({ kind: "edit", automation })}
                  />
                ))}
              </ul>
            )}
          </Card>
        ) : (
          <TemplatesTab
            onUse={(template) => {
              setEditor({ kind: "template", template });
              setTab("automations");
            }}
          />
        )}
      </div>

      <Sheet
        open={editor !== null}
        onClose={() => setEditor(null)}
        title={
          editor?.kind === "edit"
            ? "Edit automation"
            : editor?.kind === "template"
              ? "New automation from template"
              : "New automation"
        }
      >
        {editor && <AutomationEditor seed={editor} onClose={() => setEditor(null)} />}
      </Sheet>
    </div>
  );
}
