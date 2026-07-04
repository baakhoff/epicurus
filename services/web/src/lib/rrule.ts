/**
 * Repeat-picker mapping (#471): friendly presets ↔ RFC 5545 RRULE strings.
 *
 * Backs the shared repeat picker the shell's SchemaForm renders for any tool field marked
 * `format: "rrule"` — used by both the tasks form and the calendar event form. The picker's
 * submitted value is always a bare RRULE string (no `RRULE:` prefix), so the agent-facing
 * tool surface is unchanged; the picker is purely a friendlier way to author one.
 */

export type RepeatPresetId =
  | "none"
  | "daily"
  | "weekdays"
  | "weekly"
  | "monthly"
  | "yearly"
  | "custom";

export interface RepeatPreset {
  id: RepeatPresetId;
  label: string;
  /** The canonical RRULE this preset writes. Empty for `none` (one-off) and `custom` (freeform). */
  rrule: string;
}

/** Picker options, in display order. `none` first (the default for a new item), `custom` last. */
export const REPEAT_PRESETS: readonly RepeatPreset[] = [
  { id: "none", label: "Does not repeat", rrule: "" },
  { id: "daily", label: "Daily", rrule: "FREQ=DAILY" },
  { id: "weekdays", label: "Every weekday (Mon–Fri)", rrule: "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR" },
  { id: "weekly", label: "Weekly", rrule: "FREQ=WEEKLY" },
  { id: "monthly", label: "Monthly", rrule: "FREQ=MONTHLY" },
  { id: "yearly", label: "Yearly", rrule: "FREQ=YEARLY" },
  { id: "custom", label: "Custom…", rrule: "" },
];

/**
 * Normalize an RRULE for order-insensitive comparison: strip a leading `RRULE:`, uppercase
 * each `KEY=VALUE`, and sort the parts. `FREQ=WEEKLY;BYDAY=mo,tu` and `BYDAY=MO,TU;FREQ=WEEKLY`
 * both normalize equal, so a preset written by another client still preselects correctly.
 */
export function normalizeRule(rule: string): string {
  const body = rule.replace(/^RRULE:/i, "").trim();
  if (!body) return "";
  return body
    .split(";")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const eq = part.indexOf("=");
      if (eq < 0) return part.toUpperCase();
      return `${part.slice(0, eq).toUpperCase()}=${part.slice(eq + 1).toUpperCase()}`;
    })
    .sort()
    .join(";");
}

/**
 * The preset a rule maps to when prefilling the picker: `none` for empty, the matching named
 * preset when a rule equals one canonically, else `custom` (any other non-empty rule). This is
 * what lets editing a task/event open the picker on the right option instead of a blank box.
 */
export function presetForRule(rule: string): RepeatPresetId {
  const normalized = normalizeRule(rule);
  if (!normalized) return "none";
  const match = REPEAT_PRESETS.find(
    (preset) =>
      preset.id !== "none" && preset.id !== "custom" && normalizeRule(preset.rrule) === normalized,
  );
  return match ? match.id : "custom";
}

/** A short human label for a rule — `Weekly` / `Every weekday` / `Custom` — for read-only display. */
export function repeatLabel(rule: string): string {
  const id = presetForRule(rule);
  if (id === "none") return "";
  if (id === "custom") return "Custom";
  return REPEAT_PRESETS.find((preset) => preset.id === id)!.label;
}
