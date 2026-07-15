/** Shared helpers for agent-proposed changes (the review/suggestions feed, #KB-refactor). */
import type { ReviewSuggestion } from "@/lib/contracts";

/** Human verb for a proposed operation — "the assistant wants to {verb} {target}". */
export const SUGGESTION_VERB: Record<ReviewSuggestion["operation"], string> = {
  create: "add",
  update: "edit",
  append: "append to",
  delete: "delete",
  move: "move",
  mkdir: "add a folder",
  mkproject: "add a knowledge base",
};

/** The path a suggestion targets — `from → to` for a move, else the plain path. */
export function suggestionTarget(
  s: Pick<ReviewSuggestion, "operation" | "path" | "to_path">,
): string {
  return s.operation === "move" ? `${s.path} → ${s.to_path}` : s.path;
}

/** Badge tone per operation — additive/structural green, removal danger, edit accent. */
export function operationTone(op: ReviewSuggestion["operation"]): "ok" | "accent" | "danger" {
  if (op === "create" || op === "mkdir" || op === "mkproject") return "ok";
  if (op === "delete") return "danger";
  return "accent";
}

/**
 * The reserved pseudo-module the core answers in-process (ADR-0093 §2) — its own base
 * instructions and playbooks. It rides the modules list so its `review` page renders through
 * the same components as any module's, but it is the platform, not something installed.
 */
export const CORE_MODULE = "core";

/**
 * Whether review is **mandatory** for `module`, i.e. its per-module review toggle must not be
 * offered. True only for the core: ADR-0093's hard non-goal is that the agent's own guidance
 * never self-applies and no path bypasses the operator's Approve. A toggle reading "review off —
 * the agent's changes apply automatically" would advertise a bypass that does not exist (the
 * core refuses the write with a 403), so it isn't rendered.
 */
export function reviewIsMandatory(module: string): boolean {
  return module === CORE_MODULE;
}

/** Title-case a module's technical name for display ("knowledge" → "Knowledge"). */
export function moduleLabel(name: string): string {
  return name ? name[0].toUpperCase() + name.slice(1) : name;
}

/** A compact "Jun 24, 10:30" for when a suggestion was proposed (falls back to the raw ISO). */
export function formatWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
