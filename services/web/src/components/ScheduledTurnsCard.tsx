/** Scheduled turns — recurring prompts that run unattended and deliver into their own chat
 *  session (ADR-0092). Shell-rendered Settings surface, not a module page (ADR-0018): the
 *  feature lives entirely in the core, so there is no module UI to gate this behind. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { Button, Card, EmptyState, Select, Spinner, Switch, TextArea } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import type { ScheduledTurn } from "@/lib/contracts";

const SCHEDULED_TURNS_KEY = ["scheduled-turns"];
const WEEKDAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const HOUR_OPTIONS = Array.from({ length: 24 }, (_, h) => h);

function formatHour(hour: number): string {
  return `${String(hour).padStart(2, "0")}:00`;
}

/** "Daily at 07:00" / "Weekly on Monday at 07:00". */
function cadenceLabel(turn: ScheduledTurn): string {
  if (turn.cadence === "weekly") {
    const day = turn.weekday != null ? WEEKDAY_LABELS[turn.weekday] : "?";
    return `Weekly on ${day} at ${formatHour(turn.hour)}`;
  }
  return `Daily at ${formatHour(turn.hour)}`;
}

/** A compact "Jul 12, 07:00" for the last-run timestamp (falls back to the raw ISO). */
function formatWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function lastRunSummary(turn: ScheduledTurn): string {
  if (!turn.last_run_at) return "Never run yet";
  const when = formatWhen(turn.last_run_at);
  if (turn.last_status === "ok") return `Last ran ${when}`;
  if (turn.last_status === "skipped (paused)") return `Skipped ${when} — runtime was paused`;
  return `Failed ${when}${turn.last_status ? ` — ${turn.last_status}` : ""}`;
}

function NewScheduledTurnForm({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const [prompt, setPrompt] = useState("");
  const [cadence, setCadence] = useState<"daily" | "weekly">("daily");
  const [hour, setHour] = useState(7);
  const [weekday, setWeekday] = useState(0);

  const create = useMutation({
    mutationFn: () =>
      api.createScheduledTurn({
        prompt: prompt.trim(),
        cadence,
        hour,
        weekday: cadence === "weekly" ? weekday : null,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: SCHEDULED_TURNS_KEY });
      onDone();
    },
  });

  return (
    <form
      className="flex flex-col gap-3 rounded-(--radius-card) border border-edge bg-surface-2 p-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (prompt.trim()) create.mutate();
      }}
    >
      <div>
        <label className="mb-1 block text-xs text-ink-dim" htmlFor="scheduled-turn-prompt">
          Prompt
        </label>
        <TextArea
          id="scheduled-turn-prompt"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={3}
          placeholder="e.g. Summarize today's calendar, unread mail, and due tasks"
        />
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="mb-1 block text-xs text-ink-dim" htmlFor="scheduled-turn-cadence">
            Cadence
          </label>
          <Select
            id="scheduled-turn-cadence"
            value={cadence}
            onChange={(e) => setCadence(e.target.value as "daily" | "weekly")}
          >
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
          </Select>
        </div>
        {cadence === "weekly" && (
          <div>
            <label className="mb-1 block text-xs text-ink-dim" htmlFor="scheduled-turn-weekday">
              On
            </label>
            <Select
              id="scheduled-turn-weekday"
              value={weekday}
              onChange={(e) => setWeekday(Number(e.target.value))}
            >
              {WEEKDAY_LABELS.map((label, i) => (
                <option key={label} value={i}>
                  {label}
                </option>
              ))}
            </Select>
          </div>
        )}
        <div>
          <label className="mb-1 block text-xs text-ink-dim" htmlFor="scheduled-turn-hour">
            At
          </label>
          <Select
            id="scheduled-turn-hour"
            value={hour}
            onChange={(e) => setHour(Number(e.target.value))}
          >
            {HOUR_OPTIONS.map((h) => (
              <option key={h} value={h}>
                {formatHour(h)}
              </option>
            ))}
          </Select>
        </div>
      </div>
      {create.isError && (
        <p className="text-xs text-danger">
          {create.error instanceof ApiError ? create.error.detail : "Could not save."}
        </p>
      )}
      <div className="flex items-center gap-2">
        <Button type="submit" variant="primary" busy={create.isPending} disabled={!prompt.trim()}>
          Save
        </Button>
        <Button type="button" variant="ghost" onClick={onDone} disabled={create.isPending}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

/** One scheduled turn: cadence + last-run summary, an enable/disable Switch, and delete. */
function ScheduledTurnRow({ turn }: { turn: ScheduledTurn }) {
  const qc = useQueryClient();
  const refresh = () => qc.invalidateQueries({ queryKey: SCHEDULED_TURNS_KEY });

  const setEnabled = useMutation({
    mutationFn: (enabled: boolean) => api.setScheduledTurnEnabled(turn.id, enabled),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: () => api.deleteScheduledTurn(turn.id),
    onSuccess: refresh,
  });

  return (
    <li className="flex flex-col gap-2 rounded-(--radius-card) border border-edge bg-surface p-3">
      <div className="flex items-start justify-between gap-3">
        <p className="min-w-0 flex-1 text-sm text-ink">{turn.prompt}</p>
        <div className="flex shrink-0 items-center gap-2">
          <Switch
            checked={turn.enabled}
            onChange={(next) => setEnabled.mutate(next)}
            disabled={setEnabled.isPending}
            label={`Enable scheduled turn: ${turn.prompt}`}
          />
          <Button
            variant="ghost"
            onClick={() => remove.mutate()}
            disabled={remove.isPending}
            className="px-2"
            aria-label="Delete scheduled turn"
          >
            <Trash2 size={14} />
          </Button>
        </div>
      </div>
      <p className="text-xs text-ink-faint">
        {cadenceLabel(turn)} · {lastRunSummary(turn)}
      </p>
      {(setEnabled.isError || remove.isError) && (
        <p className="text-xs text-danger">Something went wrong — try again.</p>
      )}
    </li>
  );
}

export function ScheduledTurnsCard() {
  const [showForm, setShowForm] = useState(false);
  const turns = useQuery({ queryKey: SCHEDULED_TURNS_KEY, queryFn: api.scheduledTurns });

  return (
    <Card>
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="font-serif text-base text-ink">Scheduled turns</h3>
        {!showForm && (
          <Button variant="outline" onClick={() => setShowForm(true)} className="gap-1.5">
            <Plus size={13} />
            New
          </Button>
        )}
      </div>
      <p className="mb-4 text-sm text-ink-dim">
        Recurring prompts that run on their own — daily or weekly, at a local hour — and land in
        their own chat session, same as any turn you'd run yourself.
      </p>
      {showForm && (
        <div className="mb-4">
          <NewScheduledTurnForm onDone={() => setShowForm(false)} />
        </div>
      )}
      {turns.isLoading ? (
        <Spinner />
      ) : turns.isError ? (
        <p className="text-sm text-danger">Could not load scheduled turns.</p>
      ) : turns.data && turns.data.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {turns.data.map((t) => (
            <ScheduledTurnRow key={t.id} turn={t} />
          ))}
        </ul>
      ) : (
        !showForm && (
          <EmptyState quote="Nothing scheduled yet.">
            <p className="text-sm text-ink-dim">
              Add one to have the assistant check in on its own.
            </p>
          </EmptyState>
        )
      )}
    </Card>
  );
}
