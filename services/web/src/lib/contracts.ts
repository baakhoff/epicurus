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
  global_embed_default: z.string().nullable(),
  // Operator-chosen Ollama context window (num_ctx); null = the env/runtime default.
  global_context_window: z.number().nullable(),
  hidden: z.array(z.string()),
});
export type LlmPrefs = z.infer<typeof LlmPrefs>;

/** The operator's IANA timezone (ADR-0039); used by the agent's `now` tool. */
export const TimezonePrefs = z.object({ timezone: z.string() });
export type TimezonePrefs = z.infer<typeof TimezonePrefs>;

/* ── system / GPU info (context-window suggestion) ────────────────────────── */

/** A detected GPU. `vram_free_mb` is null when the vendor can't report it. */
export const GpuInfo = z.object({
  vendor: z.string(),
  name: z.string(),
  vram_total_mb: z.number(),
  vram_free_mb: z.number().nullish(),
});
export type GpuInfo = z.infer<typeof GpuInfo>;

/** The currently-effective chat model and its on-disk size. */
export const ModelSize = z.object({
  name: z.string(),
  size_mb: z.number().nullish(),
});
export type ModelSize = z.infer<typeof ModelSize>;

/** A suggested context-window range (an estimate, not a hard maximum). */
export const SuggestedContext = z.object({
  min: z.number(),
  suggested: z.number(),
  max: z.number(),
});
export type SuggestedContext = z.infer<typeof SuggestedContext>;

/** Host system + GPU snapshot backing the context-window suggestion. */
export const SystemInfo = z.object({
  gpu: GpuInfo.nullish(),
  ram_total_mb: z.number().nullish(),
  model: ModelSize.nullish(),
  suggested_context: SuggestedContext.nullish(),
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

/**
 * The assistant turn's process — its thinking and its tool steps — persisted alongside the
 * message so the folded activity timeline survives a reopen, not only the live stream
 * (ADR-0041). Null on user messages and on pre-v0.19 assistant rows.
 */
export const MessageActivity = z.object({
  thinking: z.string().default(""),
  steps: z.array(ToolStep).default([]),
});
export type MessageActivity = z.infer<typeof MessageActivity>;

export const MessageRecord = z.object({
  role: z.string(),
  content: z.string(),
  created_at: z.coerce.date(),
  entity_refs: z.array(EntityRef).default([]),
  attachments: z.array(Attachment).default([]),
  activity: MessageActivity.nullish(),
});
export type MessageRecord = z.infer<typeof MessageRecord>;

/** One remembered snippet in the Memory view — `score` is set only for search results. */
export const MemoryItem = z.object({
  id: z.number(),
  session_id: z.string(),
  role: z.string().default(""),
  text: z.string(),
  created_at: z.coerce.date().nullish(),
  score: z.number().nullish(),
});
export type MemoryItem = z.infer<typeof MemoryItem>;

/** A page of remembered snippets plus the full corpus size (so the UI can show the rest). */
export const MemoryListing = z.object({
  items: z.array(MemoryItem),
  total: z.number(),
});
export type MemoryListing = z.infer<typeof MemoryListing>;

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
export const AgentEvent = z.object({
  // `thinking` carries a chain-of-thought token, shown in the activity timeline (ADR-0041).
  type: z.enum(["delta", "tool", "done", "error", "readiness", "thinking"]),
  text: z.string().nullish(),
  tool: z.string().nullish(),
  status: z.enum(["running", "ok", "error"]).nullish(),
  turn: AgentTurn.nullish(),
  detail: z.string().nullish(),
  // Present on `readiness` events that lead a streaming turn (ADR-0027).
  readiness: Readiness.nullish(),
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
export const PageArchetype = z.enum(["browser", "calendar", "editor", "board", "review"]);
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

/** One document or folder in an `editor` page's tree (content fetched lazily on open). */
export const EditorDoc = z.object({
  id: z.string(),
  title: z.string(),
  path: z.string(),
  /** Whether this entry is a file or a directory (#216). */
  type: z.enum(["file", "dir"]).default("file"),
});
export type EditorDoc = z.infer<typeof EditorDoc>;

/** The `editor` archetype's list contract: the browsable document/folder tree. */
export const EditorData = z.object({
  title: z.string().default("Knowledge"),
  docs: z.array(EditorDoc).default([]),
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

/* ── review queue (ADR-0033, #220) ───────────────────────────────────────── */

/**
 * One pending agent-proposed change in a `review` page. The module supplies a
 * server-computed unified `diff` (current vault content → proposed); the shell
 * renders it and the approve/reject controls. Approve applies + indexes; reject
 * discards — nothing the agent proposes lands without the operator's approval.
 */
export const ReviewSuggestion = z.object({
  id: z.string(),
  title: z.string(),
  path: z.string(),
  operation: z.enum(["create", "update", "delete"]),
  origin: z.string().default("agent"),
  note: z.string().default(""),
  created_at: z.string(),
  diff: z.string().default(""),
});
export type ReviewSuggestion = z.infer<typeof ReviewSuggestion>;

/** The `review` archetype's data contract: the queue of pending suggestions. */
export const ReviewData = z.object({
  title: z.string().default("Suggestions"),
  suggestions: z.array(ReviewSuggestion).default([]),
});
export type ReviewData = z.infer<typeof ReviewData>;

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

/** An email shown in the panel's `email-reader` view (used by 3.8 mail). */
export const EmailMessage = z.object({
  subject: z.string().default("(no subject)"),
  from: z.string().nullish(),
  date: z.string().nullish(),
  body: z.string().default(""),
  /** Owning module + id, so the reader can invoke this message's actions and re-fetch itself. */
  module: z.string().default("mail"),
  message_id: z.string().default(""),
  /** Current read state — drives the status line and which toggle the reader shows. */
  unread: z.boolean().default(false),
  /** Tool-backed actions on this message (ADR-0024) — e.g. Mark as read / Mark as unread. */
  actions: z.array(BoardAction).default([]),
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
export type OAuthClientStatus = z.infer<typeof OAuthClientStatus>;
