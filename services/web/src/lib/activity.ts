/**
 * Pure helpers for the chat process display (#121) and the readiness bar (#122).
 * Kept free of React so they unit-test in isolation.
 */
import type { Readiness } from "@/lib/contracts";

/** A friendly verb for a tool's leading action word. */
const ACTION_VERBS: Record<string, string> = {
  search: "Searching",
  list: "Reading",
  get: "Reading",
  read: "Reading",
  fetch: "Reading",
  add: "Adding to",
  create: "Creating in",
  new: "Creating in",
  complete: "Updating",
  update: "Updating",
  set: "Updating",
  send: "Sending",
  delete: "Removing from",
  remove: "Removing from",
};

/**
 * Humanize a raw tool name into a short activity label.
 *
 * Tool names are `domain.action` or `domain_action` (e.g. `knowledge_search`,
 * `calendar.list_events`). When the action's first word maps to a known verb we phrase
 * it naturally ("Searching knowledge"); otherwise we fall back to a clean "Calling …".
 */
export function toolLabel(tool: string): string {
  const parts = tool.split(/[._]/).filter(Boolean);
  if (parts.length === 0) return tool;
  const [domain, ...action] = parts;
  const verb = ACTION_VERBS[(action[0] ?? "").toLowerCase()];
  if (verb) return `${verb} ${domain}`;
  return `Calling ${parts.join(" ")}`;
}

/** A short phrase for one readiness component still warming up. */
const COMPONENT_PHRASE: Record<string, string> = {
  model: "Warming the model",
  modules: "Starting modules",
};

/** A one-line summary of what the system is doing while a turn warms up. */
export function readinessSummary(readiness: Readiness): string {
  if (readiness.power === "paused") return "Asleep — wake to answer locally";
  const pending = readiness.components.filter((c) => !c.ready);
  if (pending.length === 0) return "Ready";
  return pending.map((c) => COMPONENT_PHRASE[c.name] ?? `Starting ${c.name}`).join(" · ");
}

/**
 * A 0..1 progress fraction for the readiness bar — the share of components ready, with a
 * visible floor so the bar never reads as empty while work is genuinely happening.
 */
export function readinessProgress(readiness: Readiness): number {
  if (readiness.ready) return 1;
  if (readiness.components.length === 0) return 0.15;
  const ready = readiness.components.filter((c) => c.ready).length;
  return Math.max(0.15, ready / readiness.components.length);
}
