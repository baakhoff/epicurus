/** Small display formatters. */

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || bytes <= 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

export function relativeTime(date: Date): string {
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return date.toLocaleDateString();
}

/** The conversation-list recency groups, in display order. */
export const RECENCY_BUCKETS = ["Today", "Yesterday", "This week", "This month", "Earlier"] as const;
export type RecencyBucket = (typeof RECENCY_BUCKETS)[number];

/**
 * Which recency group a moment belongs to. "Today"/"Yesterday" compare calendar days in
 * local time (a chat from 23:50 last night is "Yesterday" even ten minutes later); the
 * wider buckets are rolling windows of 7 and 30 days. `now` is injectable for tests.
 */
export function recencyBucket(date: Date, now: Date = new Date()): RecencyBucket {
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const day = 86_400_000;
  const days = Math.floor((startOfDay(now) - startOfDay(date)) / day);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "This week";
  if (days < 30) return "This month";
  return "Earlier";
}

/** A readable name for a provider alias. */
export const PROVIDER_LABELS: Record<string, string> = {
  local: "Local (Ollama)",
  claude: "Anthropic Claude",
  gpt: "OpenAI",
  grok: "xAI Grok",
  deepseek: "DeepSeek",
  gemini: "Google Gemini",
  custom: "Any OpenAI-compatible",
};

/** Example model for each hosted provider (hint text only, never enforced). */
export const PROVIDER_MODEL_HINTS: Record<string, string> = {
  claude: "claude/claude-sonnet-4-6",
  gpt: "gpt/gpt-5.2",
  grok: "grok/grok-4",
  deepseek: "deepseek/deepseek-chat",
  gemini: "gemini/gemini-3-pro",
  custom: "custom/your-model-id",
};

/** The known hosted (non-local) provider aliases — mirrors the core's provider registry
 *  (`epicurus_core_app.llm.providers.PROVIDERS`, minus the local runtime). */
export const HOSTED_PROVIDER_ALIASES: ReadonlySet<string> = new Set(
  Object.keys(PROVIDER_LABELS).filter((alias) => alias !== "local"),
);

/**
 * Whether a model id targets a hosted provider — a known, non-local `<provider>/…` prefix.
 * Mirrors the core's `providers.is_hosted`: `claude/…` is hosted, while a bare name, the
 * explicit `local/…` alias, and an unknown `hf.co/org/model:tag` prefix are all local. This
 * replaces the old `includes("/")` heuristic that mis-filed local `hf.co/…` models as hosted (#496).
 */
export function isHostedModelId(model: string): boolean {
  const slash = model.indexOf("/");
  if (slash <= 0) return false;
  return HOSTED_PROVIDER_ALIASES.has(model.slice(0, slash));
}
