/**
 * Zod mirrors of the core's /platform/v1 payloads — validated at the trust
 * boundary so a contract drift fails loudly here, not deep in a component.
 */
import { z } from "zod";

export const PowerState = z.enum(["active", "idle", "paused"]);
export type PowerState = z.infer<typeof PowerState>;

export const PowerStatus = z.object({ state: PowerState });

export const ModelInfo = z.object({
  name: z.string(),
  size: z.number().nullish(),
  loaded: z.boolean().default(false),
});
export type ModelInfo = z.infer<typeof ModelInfo>;

export const ProviderInfo = z.object({
  alias: z.string(),
  local: z.boolean(),
  configured: z.boolean(),
  needs_base_url: z.boolean().default(false),
});
export type ProviderInfo = z.infer<typeof ProviderInfo>;

export const SessionSummary = z.object({
  id: z.string(),
  title: z.string(),
  message_count: z.number(),
  last_at: z.coerce.date(),
});
export type SessionSummary = z.infer<typeof SessionSummary>;

export const MessageRecord = z.object({
  role: z.string(),
  content: z.string(),
  created_at: z.coerce.date(),
});
export type MessageRecord = z.infer<typeof MessageRecord>;

export const AgentTurn = z.object({
  content: z.string(),
  tools_used: z.array(z.string()),
  stopped: z.string(),
});
export type AgentTurn = z.infer<typeof AgentTurn>;

/** One SSE event of a streaming agent turn (event name == `type`). */
export const AgentEvent = z.object({
  type: z.enum(["delta", "tool", "done", "error"]),
  text: z.string().nullish(),
  tool: z.string().nullish(),
  status: z.enum(["running", "ok", "error"]).nullish(),
  turn: AgentTurn.nullish(),
  detail: z.string().nullish(),
});
export type AgentEvent = z.infer<typeof AgentEvent>;

export const PullProgress = z.object({
  status: z.string().default(""),
  total: z.number().nullish(),
  completed: z.number().nullish(),
});
export type PullProgress = z.infer<typeof PullProgress>;

/* ── module manifests (ADR-0007 Tier 1) ─────────────────────────────────── */

export const ToolSpec = z.object({
  name: z.string(),
  description: z.string().default(""),
  input_schema: z.record(z.string(), z.unknown()).default({}),
});
export type ToolSpec = z.infer<typeof ToolSpec>;

export const EventSpec = z.object({
  subject: z.string(),
  description: z.string().default(""),
});

export const UiAction = z.object({
  tool: z.string(),
  label: z.string(),
  description: z.string().default(""),
  intent: z.enum(["default", "primary", "danger"]).default("default"),
  confirm: z.string().nullish(),
});
export type UiAction = z.infer<typeof UiAction>;

export const UiSection = z.object({
  ui_version: z.string().default("1"),
  icon: z.string().default("puzzle"),
  summary: z.string().default(""),
  config_schema: z.record(z.string(), z.unknown()).nullish(),
  actions: z.array(UiAction).default([]),
  status_url: z.string().nullish(),
  ui_url: z.string().nullish(),
});
export type UiSection = z.infer<typeof UiSection>;

export const ModuleManifest = z.object({
  name: z.string(),
  version: z.string(),
  description: z.string().default(""),
  contract_version: z.string().default("0.1"),
  tools: z.array(ToolSpec).default([]),
  events_emitted: z.array(EventSpec).default([]),
  events_consumed: z.array(EventSpec).default([]),
  config: z.array(z.string()).default([]),
  secrets: z.array(z.string()).default([]),
  ui: UiSection.nullish(),
});
export type ModuleManifest = z.infer<typeof ModuleManifest>;

export const ModuleSnapshot = z.object({
  manifest: ModuleManifest,
  status: z.object({
    healthy: z.boolean(),
    version: z.string().nullish(),
  }),
});
export type ModuleSnapshot = z.infer<typeof ModuleSnapshot>;

export const PlatformInfo = z.object({
  contract_version: z.string(),
  core_version: z.string(),
  tenant: z.string(),
});
export type PlatformInfo = z.infer<typeof PlatformInfo>;

/* ── OAuth ───────────────────────────────────────────────────────────────── */

export const OAuthConnectResponse = z.object({ auth_url: z.string() });

export const OAuthStatus = z.object({
  provider: z.string(),
  connected: z.boolean(),
  scope: z.string().nullish(),
});
export type OAuthStatus = z.infer<typeof OAuthStatus>;

export const OAuthClientStatus = z.object({
  provider: z.string(),
  configured: z.boolean(),
});
export type OAuthClientStatus = z.infer<typeof OAuthClientStatus>;
