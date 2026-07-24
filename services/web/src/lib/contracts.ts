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
  // What the runtime reports the model can do (e.g. "tools", "vision"); only populated when
  // the list is fetched with `?capabilities=true`, empty otherwise.
  capabilities: z.array(z.string()).default([]),
  // The model's trained maximum context (#618); same opt-in as `capabilities` — null when not
  // requested or not reported, never a fake default.
  context_length: z.number().nullish(),
});
export type ModelInfo = z.infer<typeof ModelInfo>;

/** Per-model tuning; null on a field means "inherit" the global / env default. */
export const ModelSettings = z.object({
  context_window: z.number().nullable(),
  keep_alive: z.string().nullable(),
  // "gpu" | "cpu" | null (auto). Where the model runs (Ollama num_gpu); local models only.
  device: z.string().nullable(),
});
export type ModelSettings = z.infer<typeof ModelSettings>;

/** Read-only facts about a local model from the runtime's `/api/show`. */
export const ModelDetails = z.object({
  // Weight quantization is fixed at pull time (e.g. "Q4_K_M") — not a runtime knob.
  quantization: z.string().nullish(),
  parameter_size: z.string().nullish(),
  // The model's trained maximum context (a ceiling for the operator's choice).
  context_length: z.number().nullish(),
  family: z.string().nullish(),
  // Reported capabilities (e.g. "tools", "vision"); drives the chat "can't use tools" hint.
  capabilities: z.array(z.string()).default([]),
});
export type ModelDetails = z.infer<typeof ModelDetails>;

/** One pullable quantization of a model size, from the registry lookup (#330). */
export const ModelVariant = z.object({
  tag: z.string(),
  // Parsed quant label ("q8_0" / "fp16"); "" for the default build (no quant token in the tag).
  quant: z.string().default(""),
  // Real on-disk size parsed from the tags page (#571); null/absent when upstream shows none
  // (cloud aliases) or the core predates the field — the UI then falls back to its estimate.
  size_gb: z.number().nullish(),
});
export type ModelVariant = z.infer<typeof ModelVariant>;

/** The quant variants available for a model. Empty when the lookup found none / is unwired. */
export const ModelVariants = z.object({
  model: z.string(),
  variants: z.array(ModelVariant).default([]),
});
export type ModelVariants = z.infer<typeof ModelVariants>;

export const LlmPrefs = z.object({
  global_default: z.string().nullable(),
  global_embed_default: z.string().nullable(),
  // Operator-chosen Ollama context window (num_ctx); null = the env/runtime default.
  global_context_window: z.number().nullable(),
  // Operator-chosen Ollama KV-cache type ("f16"|"q8_0"|"q4_0"); null = runtime default.
  kv_cache_type: z.string().nullable(),
  // Operator-chosen agent loop bound (tool rounds per turn); null = the env default.
  global_agent_max_steps: z.number().nullable(),
  hidden: z.array(z.string()),
});
export type LlmPrefs = z.infer<typeof LlmPrefs>;

/** The operator's IANA timezone (ADR-0039); used by the agent's `now` tool. */
export const TimezonePrefs = z.object({ timezone: z.string() });
export type TimezonePrefs = z.infer<typeof TimezonePrefs>;

/** The operator's stored left-nav page order (#543) — `order` is `[]` when unset. */
export const PageOrderPrefs = z.object({ order: z.array(z.string()) });
export type PageOrderPrefs = z.infer<typeof PageOrderPrefs>;

/** One category's push / notification-center toggle pair (#670/#671, ADR-0102) — shared by
 *  both features so a category can be silenced for push while still landing in the center. */
export const ChannelPrefs = z.object({ push: z.boolean(), center: z.boolean() });
export type ChannelPrefs = z.infer<typeof ChannelPrefs>;

/**
 * Web push preferences (#670, ADR-0102): per-category toggles + quiet hours, in the
 * operator's configured timezone (ADR-0039). `known_categories` is server-supplied so the
 * settings UI never hardcodes the taxonomy; `categories` always carries one entry per known
 * category, defaulted, so the UI renders straight from it with no client-side merge.
 */
export const PushPrefs = z.object({
  categories: z.record(z.string(), ChannelPrefs),
  known_categories: z.array(z.object({ id: z.string(), label: z.string() })),
  quiet_hours_enabled: z.boolean(),
  quiet_hours_start: z.string(),
  quiet_hours_end: z.string(),
});
export type PushPrefs = z.infer<typeof PushPrefs>;

/** A persisted device subscription (#670) — named `*Record` (not `PushSubscription`) since
 *  that name is the browser's own Push API type; this is the backend's stored row, which
 *  deliberately omits the encryption keys the browser type carries. */
export const PushDeviceRecord = z.object({
  id: z.string(),
  device_label: z.string(),
  created_at: z.string(),
  last_seen_at: z.string().nullable().default(null),
});
export type PushDeviceRecord = z.infer<typeof PushDeviceRecord>;

/** Outcome of the Settings "send test notification" button (#670). */
export const PushTestResult = z.object({
  outcome: z.string(),
  sent_count: z.number(),
  pruned_count: z.number(),
});
export type PushTestResult = z.infer<typeof PushTestResult>;

/** One saved hosted-model id plus its provider alias (the id's `<provider>/` prefix) (#496). */
export const SavedHostedModel = z.object({
  model: z.string(),
  provider: z.string(),
  // From LiteLLM's model-cost map (#618); null/empty when the model isn't in that map.
  context_length: z.number().nullish(),
  capabilities: z.array(z.string()).default([]),
});
export type SavedHostedModel = z.infer<typeof SavedHostedModel>;

/** The tenant's saved hosted-model ids, most-recently-saved first (#496). */
export const SavedModelsResponse = z.object({ models: z.array(SavedHostedModel).default([]) });
export type SavedModelsResponse = z.infer<typeof SavedModelsResponse>;

/** The agent's editable base system prompt (#497). `instructions` is the effective prompt
 *  (the stored value, else the shipped default); `is_default` is true when none is stored. */
export const AgentInstructions = z.object({
  instructions: z.string(),
  is_default: z.boolean(),
});
export type AgentInstructions = z.infer<typeof AgentInstructions>;

/* ── system / GPU info (context-window suggestion) ────────────────────────── */

/** A detected GPU. `vram_free_mb` is null when the vendor can't report it. */
export const GpuInfo = z.object({
  vendor: z.string(),
  name: z.string(),
  vram_total_mb: z.number(),
  vram_free_mb: z.number().nullish(),
});
export type GpuInfo = z.infer<typeof GpuInfo>;

/** The host CPU. Core counts are null when they can't be determined. */
export const CpuInfo = z.object({
  model: z.string(),
  physical_cores: z.number().nullish(),
  logical_cores: z.number().nullish(),
});
export type CpuInfo = z.infer<typeof CpuInfo>;

/** The currently-effective chat model and the facts behind the suggestion. */
export const ModelSize = z.object({
  name: z.string(),
  size_mb: z.number().nullish(),
  // Trained maximum context (from /api/show) — the real ceiling for the suggestion.
  context_length: z.number().nullish(),
  // Weight quantization (e.g. "Q4_K_M"); fixed at pull, surfaced to explain the estimate.
  quantization: z.string().nullish(),
});
export type ModelSize = z.infer<typeof ModelSize>;

/** A suggested context-window range (an estimate, not a hard maximum). */
export const SuggestedContext = z.object({
  min: z.number(),
  suggested: z.number(),
  max: z.number(),
});
export type SuggestedContext = z.infer<typeof SuggestedContext>;

/** Host system spec + GPU snapshot backing the spec panel and context-window suggestion. */
export const SystemInfo = z.object({
  gpu: GpuInfo.nullish(),
  cpu: CpuInfo.nullish(),
  ram_total_mb: z.number().nullish(),
  model: ModelSize.nullish(),
  suggested_context: SuggestedContext.nullish(),
  // The operator's active Ollama KV-cache type, factored into the suggestion; null = default.
  kv_cache_type: z.string().nullish(),
});
export type SystemInfo = z.infer<typeof SystemInfo>;

export const ProviderInfo = z.object({
  alias: z.string(),
  local: z.boolean(),
  configured: z.boolean(),
  needs_base_url: z.boolean().default(false),
});
export type ProviderInfo = z.infer<typeof ProviderInfo>;

// One browsable, pullable model in the catalog the core parses from upstream (#269).
// `tags` stays a loose string array (not an enum) so a new upstream capability never
// fails the whole response; the UI just ignores tags it has no chip for.
export const CatalogEntry = z.object({
  id: z.string(),
  family: z.string(),
  params: z.string().default(""),
  size_gb: z.number().nullish(),
  description: z.string().default(""),
  tags: z.array(z.string()).default([]),
  pulls: z.string().nullish(),
});
export type CatalogEntry = z.infer<typeof CatalogEntry>;

// The catalog snapshot from GET /platform/v1/llm/catalog. `stale` flags a seed /
// last-good list served after a failed or skipped upstream refresh.
export const CatalogResponse = z.object({
  entries: z.array(CatalogEntry),
  source: z.string(),
  updated_at: z.coerce.date().nullable(),
  stale: z.boolean().default(false),
});
export type CatalogResponse = z.infer<typeof CatalogResponse>;

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

/**
 * One row in the in-app notification center (#671) — the durable record of every
 * push-worthy event, independent of whether push itself delivered (ADR-0102). `entity_ref`
 * is `null` until a future caller populates it (ADR-0019's hover-card contract).
 */
export const NotificationCenterItem = z.object({
  id: z.string(),
  category: z.string(),
  title: z.string(),
  body: z.string(),
  deep_link: z.string().nullable().default(null),
  entity_ref: EntityRef.nullable().default(null),
  automation_id: z.string().nullable().default(null),
  created_at: z.string(),
  read_at: z.string().nullable().default(null),
});
export type NotificationCenterItem = z.infer<typeof NotificationCenterItem>;

export const NotificationsUnreadCount = z.object({ count: z.number() });
export type NotificationsUnreadCount = z.infer<typeof NotificationsUnreadCount>;

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

/** One tool call the agent made this turn, in the activity timeline (#121, ADR-0041). */
export const ToolStep = z.object({
  tool: z.string(),
  status: z.enum(["running", "ok", "error"]).default("ok"),
  detail: z.string().nullish(),
});
export type ToolStep = z.infer<typeof ToolStep>;

/** One entry on the ordered activity timeline: a run of thinking, or a tool step (#300). */
export const ActivityItem = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("thinking"), text: z.string().default("") }),
  z.object({
    kind: z.literal("tool"),
    tool: z.string(),
    status: z.enum(["running", "ok", "error"]).default("ok"),
    detail: z.string().nullish(),
  }),
]);
export type ActivityItem = z.infer<typeof ActivityItem>;

/**
 * The assistant turn's process — its thinking and its tool steps — persisted alongside the
 * message so the folded activity timeline survives a reopen, not only the live stream
 * (ADR-0041). Null on user messages and on pre-v0.19 assistant rows.
 *
 * `timeline` is the chronological interleaving (think → call → think, #300); the flat
 * `thinking`/`steps` are kept for backward compatibility — older rows have only those, and
 * the UI falls back to them when `timeline` is empty.
 */
export const MessageActivity = z.object({
  thinking: z.string().default(""),
  steps: z.array(ToolStep).default([]),
  timeline: z.array(ActivityItem).default([]),
});
export type MessageActivity = z.infer<typeof MessageActivity>;

export const MessageRecord = z.object({
  /** The message's stable id — what `editAndRerun` names to edit a turn other than the
   *  last one (#552). Opaque: it addresses a row the session already scopes. */
  id: z.number(),
  role: z.string(),
  content: z.string(),
  created_at: z.coerce.date(),
  entity_refs: z.array(EntityRef).default([]),
  attachments: z.array(Attachment).default([]),
  activity: MessageActivity.nullish(),
});
export type MessageRecord = z.infer<typeof MessageRecord>;

/** One durable fact the assistant remembers about the user (ADR-0043).
 *  `source` is how it was learned ("tool" = the remember tool, "auto" = background
 *  extraction); `score` is set only for search results. */
export const MemoryItem = z.object({
  id: z.string(),
  text: z.string(),
  source: z.string().default("auto"),
  created_at: z.coerce.date().nullish(),
  score: z.number().nullish(),
});
export type MemoryItem = z.infer<typeof MemoryItem>;

/** A page of remembered facts plus the full corpus size (so the UI can show the rest). */
export const MemoryListing = z.object({
  items: z.array(MemoryItem),
  total: z.number(),
});
export type MemoryListing = z.infer<typeof MemoryListing>;

/** One stored version of the user's standing profile (#527, ADR-0094). `source` is "auto"
 *  (nightly synthesis) or "edited" (an operator correction, which survives re-synthesis). */
export const StandingProfile = z.object({
  id: z.number(),
  content: z.string(),
  source: z.string().default("auto"),
  created_at: z.coerce.date().nullish(),
});
export type StandingProfile = z.infer<typeof StandingProfile>;

/** The standing profile for the memory view: the current one (or null before first synthesis),
 *  whether it's a pinned operator edit, and the recent history. */
export const ProfileView = z.object({
  profile: StandingProfile.nullish(),
  source: z.string().nullish(),
  pinned: z.boolean().default(false),
  versions: z.array(StandingProfile).default([]),
});
export type ProfileView = z.infer<typeof ProfileView>;

export const AgentTurn = z.object({
  content: z.string(),
  tools_used: z.array(z.string()),
  stopped: z.string(),
  entity_refs: z.array(EntityRef).default([]),
});
export type AgentTurn = z.infer<typeof AgentTurn>;

/** One component's warming state in a readiness snapshot (ADR-0027). */
export const ReadinessComponent = z.object({
  name: z.string(),
  ready: z.boolean(),
  detail: z.string().default(""),
});
export type ReadinessComponent = z.infer<typeof ReadinessComponent>;

/** A point-in-time readiness snapshot, led on the chat stream (ADR-0027). */
export const Readiness = z.object({
  ready: z.boolean(),
  power: PowerState,
  components: z.array(ReadinessComponent).default([]),
});
export type Readiness = z.infer<typeof Readiness>;

/** One SSE event of a streaming agent turn (event name == `type`). */
/** What a document-writing tool call is writing (#541, ADR-0100/0101).
 *
 *  The core reads the module's `writes_document` annotation and pulls the named arguments off
 *  the call, so the pane can show the document *while* it is written — the module names the
 *  arguments, the shell renders (ADR-0018/0019). `target` is the document the write lands in (a
 *  path or id) and `title` a human label; either may be absent when the tool has no such
 *  argument. `content` is the body, in full and un-truncated — it rides the SSE stream only and
 *  is never persisted into the turn's activity (ADR-0041's caps are unchanged). */
export const WrittenDocument = z.object({
  module: z.string(),
  content: z.string(),
  target: z.string().nullish(),
  title: z.string().nullish(),
});
export type WrittenDocument = z.infer<typeof WrittenDocument>;

export const AgentEvent = z.object({
  // `thinking` carries a chain-of-thought token, shown in the activity timeline (ADR-0041).
  // `awaiting_input` ends the stream when the model asks a question (ADR-0053); `gone` is the
  // re-attach endpoint's sentinel for a run that has finished and been reaped (#376).
  type: z.enum([
    "delta",
    "tool",
    "done",
    "error",
    "readiness",
    "thinking",
    "awaiting_input",
    "gone",
  ]),
  text: z.string().nullish(),
  tool: z.string().nullish(),
  status: z.enum(["running", "ok", "error"]).nullish(),
  turn: AgentTurn.nullish(),
  detail: z.string().nullish(),
  // Present on `readiness` events that lead a streaming turn (ADR-0027).
  readiness: Readiness.nullish(),
  // Present on `awaiting_input` — the suspended run to resume + the question (ADR-0053).
  run_id: z.string().nullish(),
  question: z.string().nullish(),
  // Present on `awaiting_input` for a draft-review pause (ADR-0085, #563): `"draft_review"` plus
  // the composed `draft` to render in the split-pane for Confirm/Decline. Additive — a stale,
  // service-worker-cached PWA ignores the new fields and keeps parsing the stream (ADR-0055).
  awaiting_kind: z.string().nullish(),
  draft: z.record(z.string(), z.unknown()).nullish(),
  // Present on a `tool` event whose module annotated the tool `writes_document` (#541,
  // ADR-0100/0101): what the call is writing, so the shell can open the document pane. Rides
  // both the `running` and terminal frames. Additive, like the draft fields above.
  document: WrittenDocument.nullish(),
});
export type AgentEvent = z.infer<typeof AgentEvent>;

/** An in-flight turn to re-attach to, from GET /agent/sessions/{id}/active-run (#376). */
export const ActiveRun = z.object({
  run_id: z.string(),
  last_seq: z.number(),
});
export type ActiveRun = z.infer<typeof ActiveRun>;

/** Session ids with an in-flight turn, from GET /agent/active-runs — the conversations-list
 *  running indicator (#396). */
export const ActiveSessions = z.object({ session_ids: z.array(z.string()).default([]) });
export type ActiveSessions = z.infer<typeof ActiveSessions>;

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
export const PageArchetype = z.enum([
  "browser",
  "calendar",
  "editor",
  "board",
  "review",
  "mailbox",
]);
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

export const ModelSlot = z.object({
  key: z.string(),
  role: z.enum(["embedding", "chat"]),
  label: z.string(),
  description: z.string().default(""),
});
export type ModelSlot = z.infer<typeof ModelSlot>;

/* ── account/collection model (ADR-0030) ─────────────────────────────────── */

/** A module's account/collection capability: a connected-accounts picker, not a dropdown. */
export const CollectionsSpec = z.object({
  noun: z.string(),
  multi: z.boolean().default(false),
  providers: z.array(z.string()).default([]),
});
export type CollectionsSpec = z.infer<typeof CollectionsSpec>;

/** A pointer to one collection within an account (``local`` is the silent default). */
export const CollectionRef = z.object({
  account: z.string(),
  collection: z.string().default(""),
});
export type CollectionRef = z.infer<typeof CollectionRef>;

/** A collection (calendar / task list); `enabled`/`active` are filled by the core's merge. */
export const Collection = z.object({
  account: z.string(),
  collection: z.string(),
  title: z.string(),
  writable: z.boolean().default(true),
  /** Provider-supplied presentation colour (e.g. the Google calendar colour) — the shell
   *  prefers it over a derived hue so events match the user's own colours (#431). */
  color: z.string().nullish(),
  enabled: z.boolean().nullish(),
  active: z.boolean().nullish(),
});
export type Collection = z.infer<typeof Collection>;

/** One external account a module can draw collections from. */
export const Account = z.object({
  account: z.string(),
  provider: z.string(),
  label: z.string(),
  connected: z.boolean().default(false),
  collections: z.array(Collection).default([]),
});
export type Account = z.infer<typeof Account>;

/** A module's `GET /accounts` (merged) view — accounts + collections + selection. */
export const AccountsView = z.object({
  noun: z.string(),
  multi: z.boolean(),
  accounts: z.array(Account).default([]),
});
export type AccountsView = z.infer<typeof AccountsView>;

/** The operator's stored selection: enabled collections + the single active one. */
export const CollectionPrefs = z.object({
  enabled: z.array(CollectionRef).default([]),
  active: CollectionRef.nullish(),
});
export type CollectionPrefs = z.infer<typeof CollectionPrefs>;

export const ModuleManifest = z.object({
  name: z.string(),
  version: z.string(),
  description: z.string().default(""),
  contract_version: z.string().default("0.1"),
  tags: z.array(z.string()).default([]),
  tools: z.array(ToolSpec).default([]),
  events_emitted: z.array(EventSpec).default([]),
  events_consumed: z.array(EventSpec).default([]),
  config: z.array(z.string()).default([]),
  secrets: z.array(z.string()).default([]),
  ui: UiSection.nullish(),
  pages: z.array(PageSpec).default([]),
  resolver: z.boolean().default(false),
  attachable: z.boolean().default(false),
  // Model slots the operator fills per module (#128); the module fetches its choice and
  // passes it to embed/chat, falling back to the core default when unset.
  required_models: z.array(ModelSlot).default([]),
  // Account/collection model (ADR-0030): the module's connectable accounts + collections,
  // rendered as a connected-accounts section. Null when the module doesn't use the model.
  collections: CollectionsSpec.nullish(),
  // OAuth API scopes the module needs per provider (#241): {provider: [scope, …]}. The shell
  // unions these across modules and requests them at connect so a connected account grants
  // the API access its modules require.
  oauth_scopes: z.record(z.string(), z.array(z.string())).default({}),
});
export type ModuleManifest = z.infer<typeof ModuleManifest>;

export const ModuleSnapshot = z.object({
  manifest: ModuleManifest,
  status: z.object({
    healthy: z.boolean(),
    version: z.string().nullish(),
  }),
  // The operator's enable/disable choice (#126). A disabled module is hidden from the
  // agent and the left-nav but still shown on the Modules screen with a re-enable toggle.
  enabled: z.boolean().default(true),
  // Tool names the operator has explicitly disabled for this module (#213). The agent
  // never receives a disabled tool; the shell renders each as a toggleable row.
  disabled_tools: z.array(z.string()).default([]),
});
export type ModuleSnapshot = z.infer<typeof ModuleSnapshot>;

/** Whether the core can reach Docker right now, and why not (#622) — never disables module
 * removal itself (ADR-0056/#382); only container teardown + the Ollama KV-cache restart
 * defer to the next restart while `available` is false. */
export const DockerStatus = z.object({
  available: z.boolean(),
  reason: z.string().nullish(),
});
export type DockerStatus = z.infer<typeof DockerStatus>;

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
  /**
   * When true the shell offers rename/move on this entry (#391). Storage sets it for its
   * writable object entries; the read-only scanned tree leaves it false/absent. The move
   * itself goes through the shared `/pages/{pageId}/move` contract.
   */
  movable: z.boolean().nullish(),
  /**
   * When true the shell offers delete on this entry (#564). Broader than `movable`: it covers
   * directories too (delete is recursive), and it is false/absent for module-owned subtrees
   * whose lifecycle belongs to the owning page. The core Files surface sets it; module pages
   * leave it absent. The delete goes through the core `DELETE /files/entry` door, re-checked
   * server-side, so hiding the button is a courtesy, not the enforcement.
   */
  deletable: z.boolean().nullish(),
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

/** A small status pill on a board card (e.g. a due date), reusing the Badge tones. */
export const BoardBadge = z.object({
  label: z.string(),
  tone: z.enum(["dim", "accent", "ok", "warn", "danger"]).default("dim"),
});
export type BoardBadge = z.infer<typeof BoardBadge>;

/**
 * A button a `board` surfaces — board-level or per-card. Pressing it invokes the
 * module's MCP `tool` through the core, so a core-rendered board mutates with no
 * module markup. `args` are fixed values merged into every call; `form` opens a
 * SchemaForm (built from the tool's input_schema, limited to `fields`, prefilled
 * with `form_values`) before invoking; `confirm` gates a one-tap action behind a
 * dialog. A `danger` action must carry a `confirm` prompt (mirrors UiAction).
 */
export const BoardAction = z
  .object({
    tool: z.string(),
    label: z.string(),
    intent: z.enum(["default", "primary", "danger"]).default("default"),
    icon: z.string().nullish(),
    args: z.record(z.string(), z.unknown()).default({}),
    form: z.boolean().default(false),
    fields: z.array(z.string()).nullish(),
    form_values: z.record(z.string(), z.unknown()).default({}),
    /** Per-field enum options: the shell renders a <select> for any field listed here. */
    field_options: z.record(z.string(), z.array(z.string())).optional(),
    /**
     * Per-field *labeled* options: the shell renders a <select> showing `label` while
     * submitting `value`. Used where the option id isn't human-friendly — e.g. the
     * calendar / task-list picker, whose values are opaque `account:collection` tokens or
     * list ids (ADR-0030/0036/0037).
     */
    field_choices: z
      .record(z.string(), z.array(z.object({ value: z.string(), label: z.string() })))
      .optional(),
    confirm: z.string().nullish(),
    /**
     * Render this action as a compact icon-only button — the label moves to a tooltip and
     * `aria-label`. For toolbar affordances that should stay small, e.g. the board's "+"
     * Add (#337). Ignored when the action has no `icon` (there'd be nothing to show).
     */
    icon_only: z.boolean().default(false),
  })
  .superRefine((action, ctx) => {
    if (action.intent === "danger" && !action.confirm) {
      ctx.addIssue({ code: "custom", message: "a danger action must set a confirm prompt" });
    }
  });
export type BoardAction = z.infer<typeof BoardAction>;

/** One card on a `board`: a titled item with optional meta and tool-backed actions. */
export const BoardCard = z.object({
  id: z.string(),
  title: z.string(),
  subtitle: z.string().nullish(),
  body: z.string().nullish(),
  badges: z.array(BoardBadge).default([]),
  done: z.boolean().default(false),
  actions: z.array(BoardAction).default([]),
});
export type BoardCard = z.infer<typeof BoardCard>;

/** One column of a `board`. */
export const BoardColumn = z.object({
  id: z.string(),
  title: z.string(),
  cards: z.array(BoardCard).default([]),
});
export type BoardColumn = z.infer<typeof BoardColumn>;

/**
 * One declarative view control a `board` surfaces (ADR-0049): a labeled selector — e.g.
 * "Group by" (the column layout) or "Show" (a filter). The module declares the `options`
 * and the current `value`; the shell renders a selector and re-fetches the page with
 * `?<id>=<value>` on change, so regrouping/filtering stays module-side (the board carries
 * no task fields to the client). Generic and reusable across board modules.
 */
export const BoardControl = z.object({
  id: z.string(),
  label: z.string(),
  value: z.string().default(""),
  options: z.array(z.object({ value: z.string(), label: z.string() })).default([]),
});
export type BoardControl = z.infer<typeof BoardControl>;

/** The `board` archetype's data contract: columns of cards + view controls + actions. */
export const BoardData = z.object({
  title: z.string().nullish(),
  columns: z.array(BoardColumn).default([]),
  controls: z.array(BoardControl).default([]),
  actions: z.array(BoardAction).default([]),
});
export type BoardData = z.infer<typeof BoardData>;

/** A bare floating date, ``YYYY-MM-DD`` (no time, no zone). */
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/**
 * Parse a calendar event endpoint to a `Date`.
 *
 * An all-day endpoint is a **floating date** (`YYYY-MM-DD`) the module sends with no time
 * or zone; it is parsed in *local* time so it stays on its calendar date in every
 * timezone — this is the fix for all-day events rendering one day early (a date treated as
 * a UTC instant shifts back a day for negative UTC offsets). A timed endpoint is a normal
 * instant read in the viewer's local zone.
 */
export function parseEventDate(raw: string, allDay: boolean): Date {
  if (allDay && DATE_ONLY.test(raw)) {
    const [y, m, d] = raw.split("-").map(Number);
    return new Date(y, m - 1, d);
  }
  return new Date(raw);
}

/** A guest invited to an event (#432); `response_status` mirrors Google/iCalendar's
 *  vocabulary (`needsAction` / `accepted` / `declined` / `tentative`). */
export const Attendee = z.object({
  email: z.string(),
  display_name: z.string().nullish(),
  response_status: z.string().default("needsAction"),
});
export type Attendee = z.infer<typeof Attendee>;

/** One event in a `calendar` page (provider-neutral; ADR-0018). */
export const CalendarEvent = z
  .object({
    id: z.string(),
    title: z.string().default("(untitled)"),
    // Raw strings: an instant for timed events, a floating `YYYY-MM-DD` for all-day ones.
    // `parseEventDate` resolves each to a Date with the right calendar day (see transform).
    start: z.string(),
    end: z.string(),
    /** All-day (date-only) event — rendered on its date with no time, no zone shift. */
    all_day: z.boolean().default(false),
    location: z.string().nullish(),
    description: z.string().nullish(),
    provider: z.string().nullish(),
    // The calendar this event belongs to, as an `account[:collection]` token (#378) — lets the
    // shell group events by calendar and toggle each calendar's visibility. Null on a bare event.
    calendar_id: z.string().nullish(),
    // Recurrence (#432): an RFC 5545 RRULE string on a series' own event, set only on the
    // series itself — not on an expanded occurrence (each instance carries its own id and,
    // when part of a series, `recurring_event_id` instead).
    recurrence: z.string().nullish(),
    /** The series' event id, on an occurrence of a recurring event; null otherwise. */
    recurring_event_id: z.string().nullish(),
    /** Guests invited to the event (#432); empty for none. */
    attendees: z.array(Attendee).default([]),
    /** A Google Meet join link (#444); null unless attached at create time (Google-only). */
    meet_url: z.string().nullish(),
    // Per-event Edit/Delete actions (#208) — same vocabulary as board actions; the shell
    // invokes the named MCP tool through the core's tool proxy and refetches on success.
    actions: z.array(BoardAction).default([]),
  })
  .transform((ev) => ({
    ...ev,
    start: parseEventDate(ev.start, ev.all_day),
    end: parseEventDate(ev.end, ev.all_day),
  }));
export type CalendarEvent = z.infer<typeof CalendarEvent>;

/** The `calendar` archetype's data: events within the requested `[start, end)` window. */
export const CalendarData = z.object({
  title: z.string().nullish(),
  provider: z.string().nullish(),
  range: z.object({ start: z.coerce.date(), end: z.coerce.date() }).nullish(),
  events: z.array(CalendarEvent).default([]),
  // Page-level actions (#208) — e.g. "New event".
  actions: z.array(BoardAction).default([]),
});
export type CalendarData = z.infer<typeof CalendarData>;

/**
 * One date-anchored item from another module's calendar feed (#469) — e.g. an open task
 * with a due date. `date` is a floating `YYYY-MM-DD`, never timezone-shifted (same
 * convention as an all-day `CalendarEvent`). `module` is stamped by the core's aggregate
 * endpoint (`GET /platform/v1/calendar-feed`), not the owning module itself; `module` +
 * `kind` + `ref_id` route a click to that module's existing hover-card resolver
 * (`GET /resolve/{kind}/{ref_id}`, ADR-0019) generically — no new UI contract, distinct
 * from a real `CalendarEvent` (read-only, no `actions`).
 */
export const CalendarFeedItem = z.object({
  id: z.string(),
  title: z.string(),
  date: z.string(),
  status: z.string(),
  ref_id: z.string(),
  kind: z.string(),
  module: z.string(),
});
export type CalendarFeedItem = z.infer<typeof CalendarFeedItem>;

/** One document or folder in an `editor` page's tree (content fetched lazily on open). */
export const EditorDoc = z.object({
  id: z.string(),
  title: z.string(),
  path: z.string(),
  /** Whether this entry is a file or a directory (#216). */
  type: z.enum(["file", "dir"]).default("file"),
});
export type EditorDoc = z.infer<typeof EditorDoc>;

/**
 * One selectable scope in the editor's switcher (#KB-refactor): a `project` is a
 * writable knowledge base (a top-level folder); a `reference` scope (the bundled
 * platform docs) is read-only.
 */
export const EditorScope = z.object({
  id: z.string(),
  title: z.string(),
  kind: z.enum(["project", "reference"]).default("project"),
});
export type EditorScope = z.infer<typeof EditorScope>;

/** The `editor` archetype's list contract: the browsable document/folder tree. */
export const EditorData = z.object({
  title: z.string().default("Knowledge"),
  docs: z.array(EditorDoc).default([]),
  /**
   * Projects/scopes (#KB-refactor): the knowledge bases the switcher lists, the active
   * one (`docs` paths are relative to it), the noun for the "New …" control, and whether
   * the operator may create another. Empty `scope_noun` ⇒ no switcher (Notes).
   */
  scopes: z.array(EditorScope).default([]),
  scope: z.string().default(""),
  scope_noun: z.string().default(""),
  can_create_scope: z.boolean().default(false),
  /**
   * Opt into in-app authoring (ADR-0026): when true the shared editor shows a
   * "New note" affordance that saves to a fresh path. Notes sets this; knowledge
   * leaves it false (its documents are authored externally in Obsidian).
   */
  can_create: z.boolean().default(false),
  /**
   * Opt into tree management (#216): when true the shell shows folder CRUD
   * controls — create/delete folders, delete files, rename. Knowledge sets this;
   * notes does not (notes has its own `can_create` flow).
   */
  can_manage_files: z.boolean().default(false),
  /**
   * View-only mode (#232): when true the vault is externally owned — a watched
   * Obsidian-synced folder — so the editor hides Save and all authoring, and shows a
   * read-only banner. The module also leaves `can_create`/`can_manage_files` false in
   * this mode, so Obsidian stays the sole author.
   */
  read_only: z.boolean().default(false),
  /**
   * Save history (ADR-0046): when true every save snapshots the document, and the shell
   * shows a History affordance to browse and restore past versions. Notes and knowledge
   * set this. Restore is client-side (re-save a past version's content).
   */
  versioned: z.boolean().default(false),
});
export type EditorData = z.infer<typeof EditorData>;

/** One document's content, returned when the editor opens it. */
export const EditorDocContent = z.object({
  path: z.string(),
  title: z.string(),
  content: z.string(),
});
export type EditorDocContent = z.infer<typeof EditorDocContent>;

/** The result of saving an `editor` document. */
export const EditorSaveResult = z.object({
  path: z.string(),
  indexed: z.boolean().default(false),
  chunk_count: z.number().default(0),
});
export type EditorSaveResult = z.infer<typeof EditorSaveResult>;

/** One entry in an `editor` document's save history (ADR-0046) — metadata only, no body. */
export const EditorVersion = z.object({
  version_id: z.string(),
  created_at: z.string(),
  title: z.string().default(""),
  size: z.number().default(0),
});
export type EditorVersion = z.infer<typeof EditorVersion>;

/** A document's save history, newest first. */
export const EditorVersionList = z.object({
  versions: z.array(EditorVersion).default([]),
});
export type EditorVersionList = z.infer<typeof EditorVersionList>;

/** One past version's full content, fetched when the operator views it (ADR-0046). */
export const EditorVersionContent = z.object({
  path: z.string(),
  version_id: z.string(),
  created_at: z.string(),
  title: z.string().default(""),
  content: z.string(),
});
export type EditorVersionContent = z.infer<typeof EditorVersionContent>;

/* ── review queue (ADR-0033, #220) ───────────────────────────────────────── */

/**
 * One pending agent-proposed change in a `review` page. The module supplies a
 * server-computed unified `diff` (current vault content → proposed); the shell
 * renders it and the approve/reject controls. Approve applies + indexes; reject
 * discards — nothing the agent proposes lands without the operator's approval.
 */
/**
 * The structured face of an automation proposal (#667/ADR-0107). Present only when a
 * suggestion targets an automation; the modal renders this — trigger in words, filter, action,
 * autonomy, sinks — with a model picker, instead of a raw text diff. `model` is the drafted
 * per-automation model (null = the operator's default) and the one field editable pre-approval.
 */
export const AutomationPreview = z.object({
  name: z.string(),
  trigger: z.string(),
  filter: z.string().default(""),
  action: z.string(),
  autonomy: z.string(),
  autonomy_label: z.string(),
  sinks: z.array(z.string()).default([]),
  model: z.string().nullable().default(null),
});
export type AutomationPreview = z.infer<typeof AutomationPreview>;

export const ReviewSuggestion = z.object({
  id: z.string(),
  title: z.string(),
  path: z.string(),
  // Content ops carry a `diff`; structural ops (move/mkdir/mkproject) are confirmed from
  // `path`/`to_path`. `append` (notes) is content-like — its diff shows the added text.
  operation: z.enum(["create", "update", "append", "delete", "move", "mkdir", "mkproject"]),
  origin: z.string().default("agent"),
  note: z.string().default(""),
  created_at: z.string(),
  diff: z.string().default(""),
  /** Destination for a `move` (empty otherwise). */
  to_path: z.string().default(""),
  /** Full texts for the per-hunk review (#KB-refactor): `current` is the live document
   *  (empty for a create), `content` is the proposal (empty for a delete). */
  current: z.string().default(""),
  content: z.string().default(""),
  /** Set for an automation proposal (#667): render this preview + a model picker, not a diff. */
  automation: AutomationPreview.nullish(),
});
export type ReviewSuggestion = z.infer<typeof ReviewSuggestion>;

/** The `review` archetype's data contract: the queue of pending suggestions. */
export const ReviewData = z.object({
  title: z.string().default("Suggestions"),
  suggestions: z.array(ReviewSuggestion).default([]),
});
export type ReviewData = z.infer<typeof ReviewData>;

/**
 * A pending suggestion in the cross-module feed (#KB-refactor): a `ReviewSuggestion` plus
 * the module + page that owns it, so the chat composer bubble and the Suggestions page can
 * approve/reject it from anywhere. Served by `GET /platform/v1/suggestions`.
 */
export const PendingSuggestion = ReviewSuggestion.extend({
  module: z.string(),
  page_id: z.string(),
});
export type PendingSuggestion = z.infer<typeof PendingSuggestion>;

/**
 * An audit record of one resolved suggestion (ADR-0090): the proposal alongside what was
 * actually applied, including any edit the operator made before approving. The pending
 * queue drops a row on resolution (ADR-0033); this is the durable trail that replaces it.
 */
export const ReviewDecision = z.object({
  id: z.string(),
  title: z.string(),
  path: z.string(),
  operation: z.enum(["create", "update", "append", "delete", "move", "mkdir", "mkproject"]),
  origin: z.string().default("agent"),
  note: z.string().default(""),
  created_at: z.string(),
  decided_at: z.string(),
  decision: z.enum(["approved", "rejected"]),
  proposed_content: z.string().default(""),
  applied_content: z.string().default(""),
  to_path: z.string().default(""),
});
export type ReviewDecision = z.infer<typeof ReviewDecision>;

/** The resolved-decision audit trail for a review page, newest first (ADR-0090). */
export const ReviewAuditData = z.object({
  decisions: z.array(ReviewDecision).default([]),
});
export type ReviewAuditData = z.infer<typeof ReviewAuditData>;

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

/** One attachment on a message (ADR-0087) — metadata only; bytes stream via the core proxy. */
export const MailAttachment = z.object({
  id: z.string(),
  filename: z.string(),
  mime_type: z.string().default(""),
  size: z.number().default(0),
  /** An inline image's `Content-ID` (no angle brackets) — the HTML body's `cid:<id>` targets
   *  (ADR-0097, #627). Null for an ordinary attachment. */
  content_id: z.string().nullish(),
  /** Inline part (dispositioned inline or carrying a `Content-ID`): resolved for the HTML body,
   *  hidden from the download row. */
  inline: z.boolean().default(false),
});
export type MailAttachment = z.infer<typeof MailAttachment>;

/** An email shown in the panel's `email-reader` view + the mailbox thread pane (ADR-0019/0087). */
export const EmailMessage = z.object({
  subject: z.string().default("(no subject)"),
  from: z.string().nullish(),
  date: z.string().nullish(),
  body: z.string().default(""),
  /** The HTML body when the message has one (ADR-0097, #627) — rendered in a sandboxed iframe
   *  with inline images resolved through the module and remote images blocked by default; `body`
   *  (plain text) is the fallback. */
  body_html: z.string().nullish(),
  /** Owning module + id, so the reader can invoke this message's actions and re-fetch itself. */
  module: z.string().default("mail"),
  message_id: z.string().default(""),
  /** Current read state — drives the status line and which toggle the reader shows. */
  unread: z.boolean().default(false),
  /** Tool-backed actions on this message (ADR-0024) — e.g. Mark as read / Mark as unread. */
  actions: z.array(BoardAction).default([]),
  /** Attachments listed under the message; downloaded on demand via the core proxy (ADR-0087).
   *  Absent for the panel reader (which doesn't fetch them), present in the mailbox thread. */
  attachments: z.array(MailAttachment).default([]),
});
export type EmailMessage = z.infer<typeof EmailMessage>;

/* ── mailbox archetype (ADR-0087) ────────────────────────────────────────── */

/** One folder/label in the mailbox rail; `unread` is shown only when the provider supplied it. */
export const MailLabel = z.object({
  id: z.string(),
  title: z.string(),
  kind: z.string().default("system"),
  unread: z.number().nullish(),
});
export type MailLabel = z.infer<typeof MailLabel>;

/** One row in the paginated thread list — a conversation at a glance. */
export const MailThreadSummary = z.object({
  id: z.string(),
  subject: z.string().default("(no subject)"),
  sender: z.string().default(""),
  snippet: z.string().default(""),
  date: z.string().default(""),
  unread: z.boolean().default(false),
  message_count: z.number().default(1),
  /** The thread's last-message epoch ms — the local cache's ordering key (ADR-0096, #623). */
  sort_ts: z.number().default(0),
});
export type MailThreadSummary = z.infer<typeof MailThreadSummary>;

/** The `mailbox` list read: the rail + one cursor page of threads (ADR-0087). */
export const MailboxListData = z.object({
  title: z.string().default("Mail"),
  labels: z.array(MailLabel).default([]),
  active_label: z.string().default("INBOX"),
  query: z.string().default(""),
  threads: z.array(MailThreadSummary).default([]),
  /** Opaque next-page token; absent at the end of the mailbox. */
  next_cursor: z.string().nullish(),
});
export type MailboxListData = z.infer<typeof MailboxListData>;

/** The server-derived reply prefill for a thread (ADR-0087). The actual send re-derives
 *  threading from `reply_to_message_id`, so the web never handles raw RFC-2822 headers. */
export const MailboxReply = z.object({
  reply_to_message_id: z.string(),
  to: z.string().default(""),
  subject: z.string().default(""),
  reply_to_original: z.string().nullish(),
});
export type MailboxReply = z.infer<typeof MailboxReply>;

/** The `mailbox` thread read: a full conversation + the reply prefill (ADR-0087). */
export const MailThreadData = z.object({
  id: z.string(),
  subject: z.string().default("(no subject)"),
  messages: z.array(EmailMessage).default([]),
  reply: MailboxReply.nullish(),
});
export type MailThreadData = z.infer<typeof MailThreadData>;

/** The `?thread_id=` envelope — the module returns `{thread}` for a single conversation. */
export const MailboxThreadData = z.object({ thread: MailThreadData });
export type MailboxThreadData = z.infer<typeof MailboxThreadData>;

/** A composed outbound email shown in the split-pane for Confirm/Decline (ADR-0085, #563).
 *  The same payload the module's transmit endpoint sends, so what is reviewed is what goes out. */
export const EmailDraft = z.object({
  to: z.string().default(""),
  cc: z.string().nullish(),
  subject: z.string().default("(no subject)"),
  body: z.string().default(""),
  /** For a reply, the original this answers ("sender — subject") — shown as thread context. */
  reply_to_original: z.string().nullish(),
});
export type EmailDraft = z.infer<typeof EmailDraft>;

/** A text file's contents for the right-panel `doc-reader` view (#KB-refactor, req 6). */
export const FileText = z.object({
  path: z.string(),
  name: z.string(),
  content: z.string(),
});
export type FileText = z.infer<typeof FileText>;

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

/* ── Messaging bridges (ADR-0062) ────────────────────────────────────────── */

/** One chat bridge's live state, from the messaging module via the core (#369).
 *  `configured` = a bot token is stored; `enabled` = the operator's on/off; `connected` =
 *  the live link is up. `manageable` is false for the in-process loopback bridge. */
export const BridgeStatus = z.object({
  bridge: z.string(),
  label: z.string(),
  manageable: z.boolean(),
  configured: z.boolean(),
  enabled: z.boolean(),
  connected: z.boolean(),
  detail: z.string().default(""),
});
export type BridgeStatus = z.infer<typeof BridgeStatus>;

/* ── Log stream (ADR-0031) ───────────────────────────────────────────────── */

/** One structured log entry emitted by the core (ADR-0031). */
export const LogEntry = z.object({
  ts: z.string(),
  level: z.enum(["debug", "info", "warning", "error", "critical"]),
  service: z.string().default(""),
  message: z.string(),
  context: z.record(z.string(), z.unknown()).default({}),
});
export type LogEntry = z.infer<typeof LogEntry>;

/* ── Module event spine ──────────────────────────────────────────────────── */

/**
 * One world-change a module announced, as the core's durable log recorded it.
 *
 * `payload` is pointers and metadata only — never content, never credentials (the core
 * enforces that on the way in and redacts again on the way out), so a feed row is safe to
 * render verbatim. `entity_ref` is what lets a row show an ADR-0019 hover-card chip with
 * no per-module code in the shell.
 */
export const ModuleEvent = z.object({
  id: z.number(),
  tenant: z.string(),
  module: z.string(),
  type: z.string(),
  /** When the change happened in the world (the emitting module's clock). */
  occurred_at: z.string(),
  /** When the core recorded it — not the same thing, and the feed orders by this. */
  received_at: z.string(),
  dedup_key: z.string(),
  entity_ref: EntityRef.nullish(),
  payload: z.record(z.string(), z.unknown()).default({}),
  schema_version: z.number().default(1),
});
export type ModuleEvent = z.infer<typeof ModuleEvent>;

/* ── automations (ADR-0105) ─────────────────────────────────────────────── */

/** One deterministic payload matcher in an event trigger (AND-combined). */
export const AutomationMatcher = z.object({
  field: z.string(),
  op: z.enum(["eq", "ne", "contains", "exists", "gt", "lt"]),
  value: z.unknown().nullish(),
});
export type AutomationMatcher = z.infer<typeof AutomationMatcher>;

export const AutomationEventTrigger = z.object({
  module: z.string(),
  event_type: z.string(),
  matchers: z.array(AutomationMatcher).default([]),
  window_start_hour: z.number().nullish(),
  window_end_hour: z.number().nullish(),
});
export type AutomationEventTrigger = z.infer<typeof AutomationEventTrigger>;

/** The ADR-0092 schedule vocabulary the engine reuses (weekday 0=Monday). */
export const AutomationScheduleTrigger = z.object({
  cadence: z.string(),
  hour: z.number(),
  weekday: z.number().nullish(),
});
export type AutomationScheduleTrigger = z.infer<typeof AutomationScheduleTrigger>;

/** One automation, as `GET /platform/v1/automations` returns it. */
export const Automation = z.object({
  id: z.string(),
  name: z.string(),
  enabled: z.boolean(),
  /** `user` · `agent` · `template:<module>` — where the row came from. */
  source: z.string(),
  event_trigger: AutomationEventTrigger.nullish(),
  schedule_trigger: AutomationScheduleTrigger.nullish(),
  prompt: z.string(),
  /** Blank/null = the tenant's default model (the ADR-0029 fall-through). */
  model: z.string().nullish(),
  autonomy: z.string(),
  sinks: z.array(z.string()).default([]),
  chat_mode: z.string(),
  rate_cap_per_hour: z.number(),
  digest_window_minutes: z.number(),
  created_at: z.string(),
  last_run_at: z.string().nullish(),
  last_status: z.string().nullish(),
  /** Derived server-side from the autonomy dial — never stored, never guessed here. */
  allowed_tool_classes: z.array(z.string()).default([]),
});
export type Automation = z.infer<typeof Automation>;

/** One run-ledger entry, as the runs feed renders it (#669). */
export const AutomationRun = z.object({
  id: z.string(),
  automation_id: z.string(),
  started_at: z.string(),
  /** The `module_events` row ids that caused it (empty for schedule/manual runs). */
  trigger_refs: z.array(z.number()).default([]),
  filter_verdict: z.string(),
  model: z.string().nullish(),
  prompt_tokens: z.number().nullish(),
  completion_tokens: z.number().nullish(),
  duration_ms: z.number().nullish(),
  /** `ok` · `error` · `skipped` — skips are first-class (rate cap, paused). */
  outcome: z.string(),
  error: z.string().nullish(),
  output: z.string().default(""),
  sinks_fired: z.array(z.string()).default([]),
  /** The triggering events' entity refs, server-resolved for hover-card chips. */
  trigger_entity_refs: z.array(EntityRef).default([]),
});
export type AutomationRun = z.infer<typeof AutomationRun>;

/** A module's preset automation, offered on the Templates tab — never auto-instantiated. */
export const AutomationTemplate = z.object({
  module: z.string(),
  key: z.string(),
  name: z.string(),
  description: z.string().default(""),
  trigger: z.record(z.string(), z.unknown()).default({}),
  prompt: z.string().default(""),
  autonomy: z.string().default("notify"),
  sinks: z.array(z.string()).default([]),
});
export type AutomationTemplate = z.infer<typeof AutomationTemplate>;

/** The engine's closed vocabularies, so the editor never hardcodes them. */
export const AutomationVocabulary = z.object({
  autonomy_levels: z.array(z.string()),
  sinks: z.array(z.string()),
  matcher_ops: z.array(z.string()),
});
export type AutomationVocabulary = z.infer<typeof AutomationVocabulary>;

/** The tenant-wide automations stop (Postgres-persisted — survives a restart). */
export const AutomationKillSwitch = z.object({ halted: z.boolean() });
export type AutomationKillSwitch = z.infer<typeof AutomationKillSwitch>;

/** The editor's save shape — create and update share it (`source` only on create). */
export type AutomationDraft = {
  name: string;
  prompt: string;
  autonomy: string;
  event_trigger: AutomationEventTrigger | null;
  schedule_trigger: AutomationScheduleTrigger | null;
  model: string | null;
  sinks: string[];
  chat_mode: string;
  rate_cap_per_hour: number;
  digest_window_minutes: number;
  enabled: boolean;
};

export type OAuthClientStatus = z.infer<typeof OAuthClientStatus>;

/** A registered maintenance job advertised to the UI (#383). */
export const MaintenanceJob = z.object({
  key: z.string(),
  label: z.string(),
  /** Whether the scheduled nightly batch runs it (heavy jobs are manual-only). */
  nightly: z.boolean(),
});
export type MaintenanceJob = z.infer<typeof MaintenanceJob>;

/** One job's outcome in a maintenance run. */
export const MaintenanceJobResult = z.object({
  key: z.string(),
  label: z.string(),
  status: z.enum(["ok", "skipped", "error"]),
  detail: z.string(),
});
export type MaintenanceJobResult = z.infer<typeof MaintenanceJobResult>;

/** The aggregate result of one maintenance batch (#383). */
export const MaintenanceRun = z.object({
  ran_at: z.string(),
  scope: z.string(),
  jobs: z.array(MaintenanceJobResult).default([]),
});
export type MaintenanceRun = z.infer<typeof MaintenanceRun>;

/** One job's live status within an in-flight maintenance run (#561). */
export const MaintenanceJobProgress = z.object({
  key: z.string(),
  label: z.string(),
  status: z.enum(["pending", "running", "ok", "skipped", "error"]),
  detail: z.string(),
});
export type MaintenanceJobProgress = z.infer<typeof MaintenanceJobProgress>;

/**
 * An in-flight maintenance batch — polled while live and rehydrated on mount, so a page refresh
 * mid-batch lands back on the same run instead of losing it (#561).
 */
export const MaintenanceCurrentRun = z.object({
  started_at: z.string(),
  scope: z.string(),
  jobs: z.array(MaintenanceJobProgress).default([]),
});
export type MaintenanceCurrentRun = z.infer<typeof MaintenanceCurrentRun>;

/** The maintenance surface: the schedule, the registered jobs, the last run, and any live run. */
export const MaintenanceStatus = z.object({
  schedule_enabled: z.boolean(),
  // "hourly" | "daily" | "weekly" — kept as a plain string so an older/newer core's cadence
  // this build doesn't know about still round-trips (the Select just shows it verbatim).
  schedule_cadence: z.string(),
  schedule_hour: z.number(),
  // 0=Monday..6=Sunday; only meaningful (non-null) for a "weekly" cadence.
  schedule_weekday: z.number().nullish(),
  // ISO 8601 in the tenant's timezone; null when the schedule is disabled — an estimate for
  // display, not a guarantee (the scheduler's own due-check additionally dedupes a window).
  next_run_at: z.string().nullish(),
  jobs: z.array(MaintenanceJob).default([]),
  last_run: MaintenanceRun.nullish(),
  current_run: MaintenanceCurrentRun.nullish(),
});
export type MaintenanceStatus = z.infer<typeof MaintenanceStatus>;

/** A ``PUT /maintenance/schedule`` body (#621) — validated server-side as a whole. */
export const MaintenanceScheduleUpdate = z.object({
  enabled: z.boolean(),
  cadence: z.string(),
  hour: z.number(),
  weekday: z.number().nullish(),
});
export type MaintenanceScheduleUpdate = z.infer<typeof MaintenanceScheduleUpdate>;
