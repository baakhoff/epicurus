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
  hidden: z.boolean().default(false),
});
export type ModelInfo = z.infer<typeof ModelInfo>;

export const LlmPrefs = z.object({
  global_default: z.string().nullable(),
  hidden: z.array(z.string()),
});
export type LlmPrefs = z.infer<typeof LlmPrefs>;

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

/** A reference to a module entity the assistant mentions (ADR-0019). */
export const EntityRef = z.object({
  ref_id: z.string(),
  module: z.string(),
  kind: z.string(),
  title: z.string(),
  summary: z.string().nullish(),
});
export type EntityRef = z.infer<typeof EntityRef>;

/** Context the user attached to a message (ADR-0019). */
export const Attachment = z.object({
  att_id: z.string(),
  source: z.enum(["module", "file", "chat"]),
  kind: z.string().default(""),
  ref_id: z.string().nullish(),
  title: z.string().default(""),
  module: z.string().nullish(),
});
export type Attachment = z.infer<typeof Attachment>;

/** The handle returned when a file is uploaded for attachment. */
export const AttachmentUploaded = z.object({
  att_id: z.string(),
  title: z.string(),
  kind: z.string(),
});
export type AttachmentUploaded = z.infer<typeof AttachmentUploaded>;

/** One item a module's attachment picker offers. */
export const ModuleAttachmentItem = z.object({
  ref_id: z.string(),
  kind: z.string().default(""),
  title: z.string().default(""),
});
export type ModuleAttachmentItem = z.infer<typeof ModuleAttachmentItem>;

export const MessageRecord = z.object({
  role: z.string(),
  content: z.string(),
  created_at: z.coerce.date(),
  entity_refs: z.array(EntityRef).default([]),
  attachments: z.array(Attachment).default([]),
});
export type MessageRecord = z.infer<typeof MessageRecord>;

export const AgentTurn = z.object({
  content: z.string(),
  tools_used: z.array(z.string()),
  stopped: z.string(),
  entity_refs: z.array(EntityRef).default([]),
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

/* ── module-contributed pages (ADR-0018) ─────────────────────────────────── */

/** The bounded vocabulary of core-rendered left-nav view shapes. */
export const PageArchetype = z.enum(["browser", "calendar", "editor", "board"]);
export type PageArchetype = z.infer<typeof PageArchetype>;

export const PageSpec = z.object({
  id: z.string(),
  title: z.string(),
  archetype: PageArchetype,
  icon: z.string().default("puzzle"),
  nav_order: z.number().default(100),
  capability: z.string().nullish(),
});
export type PageSpec = z.infer<typeof PageSpec>;

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
  pages: z.array(PageSpec).default([]),
  resolver: z.boolean().default(false),
  attachable: z.boolean().default(false),
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

/* ── archetype data shapes (core-rendered; the module supplies data only) ─── */

/** One row in a `browser` page: a list entry plus its detail body. */
export const BrowserItem = z.object({
  id: z.string(),
  title: z.string(),
  subtitle: z.string().nullish(),
  body: z.string().nullish(),
  icon: z.string().nullish(),
  /** URL the shell uses to navigate into a directory (directories only). */
  nav_path: z.string().nullish(),
  /** Absolute download URL proxied through the core (files only). */
  href: z.string().nullish(),
});
export type BrowserItem = z.infer<typeof BrowserItem>;

/** The `browser` archetype's data contract: a titled list + per-item detail. */
export const BrowserData = z.object({
  title: z.string().nullish(),
  items: z.array(BrowserItem).default([]),
  /** Current directory path being browsed (empty = root). */
  path: z.string().nullish(),
  /** When true the shell renders a search input above the list. */
  search_enabled: z.boolean().optional(),
});
export type BrowserData = z.infer<typeof BrowserData>;

/* ── right-panel views (ADR-0018 / ADR-0019) ─────────────────────────────── */

/** One label/value row of a hover-card / entity-detail panel. */
export const HoverCardDetail = z.object({ label: z.string(), value: z.string() });
export type HoverCardDetail = z.infer<typeof HoverCardDetail>;

/** An outbound link a hover-card may carry (e.g. to a future GitHub-issue module). */
export const HoverCardLink = z.object({ label: z.string(), url: z.string() });
export type HoverCardLink = z.infer<typeof HoverCardLink>;

/**
 * The uniform hover-card / entity-detail envelope every module entity resolves to
 * (ADR-0019). Rendered both as the inline hover-card and, in full, as the panel's
 * `entity-detail` view — one shape, core-owned.
 */
export const HoverCard = z.object({
  title: z.string(),
  description: z.string().default(""),
  details: z.array(HoverCardDetail).default([]),
  href: HoverCardLink.nullish(),
});
export type HoverCard = z.infer<typeof HoverCard>;

/** A read-only email shown in the panel's `email-reader` view (used by 3.8 mail). */
export const EmailMessage = z.object({
  subject: z.string().default("(no subject)"),
  from: z.string().nullish(),
  date: z.string().nullish(),
  body: z.string().default(""),
});
export type EmailMessage = z.infer<typeof EmailMessage>;

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
