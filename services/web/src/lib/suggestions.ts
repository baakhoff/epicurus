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
