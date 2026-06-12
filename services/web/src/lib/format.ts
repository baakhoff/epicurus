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
