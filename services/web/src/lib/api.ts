/**
 * The core API client. Same-origin: nginx (or the Vite dev proxy) forwards
 * /platform/* to the core container, so there is no CORS anywhere.
 */
import { z } from "zod";

import {
  AccountsView,
  ActiveRun,
  AttachmentUploaded,
  CatalogResponse,
  type CollectionPrefs,
  EditorDocContent,
  EditorSaveResult,
  EditorScope,
  EditorVersionContent,
  EditorVersionList,
  EmailMessage,
  FileText,
  HoverCard,
  LlmPrefs,
  LogEntry,
  MemoryListing,
  MessageRecord,
  ModelDetails,
  ModelInfo,
  ModelSettings,
  ModelVariants,
  ModuleAttachmentItem,
  ModuleSnapshot,
  OAuthClientStatus,
  OAuthConnectResponse,
  OAuthStatus,
  PendingSuggestion,
  PlatformInfo,
  PowerStatus,
  ProviderInfo,
  Readiness,
  SessionSummary,
  SystemInfo,
  TimezonePrefs,
  type PowerState,
} from "@/lib/contracts";
import { parseFrame } from "@/lib/sse";

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(detail);
  }
}

export class PausedError extends ApiError {}

async function request<T>(
  schema: z.ZodType<T>,
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    // 503 from the LLM surface means the gateway is paused — a state, not a bug.
    if (response.status === 503) throw new PausedError(503, detail);
    throw new ApiError(response.status, detail);
  }
  return schema.parse(await response.json());
}

export const api = {
  power: () => request(PowerStatus, "/platform/v1/power"),
  setPower: (state: PowerState) =>
    request(PowerStatus, "/platform/v1/power", {
      method: "PUT",
      body: JSON.stringify({ state }),
    }),

  models: (withCapabilities = false) =>
    request(
      z.array(ModelInfo),
      `/platform/v1/llm/models${withCapabilities ? "?capabilities=true" : ""}`,
    ),
  // The browsable model catalog the core parses from upstream on a schedule (#269).
  catalog: () => request(CatalogResponse, "/platform/v1/llm/catalog"),
  deleteModel: (name: string) =>
    request(z.object({ status: z.string() }), `/platform/v1/llm/models?name=${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),

  llmPrefs: () => request(LlmPrefs, "/platform/v1/llm/prefs"),
  setGlobalDefault: (model: string | null) =>
    request(z.object({ status: z.string() }), "/platform/v1/llm/prefs/default", {
      method: "PUT",
      body: JSON.stringify({ model }),
    }),
  setGlobalEmbedDefault: (model: string | null) =>
    request(z.object({ status: z.string() }), "/platform/v1/llm/prefs/embed-default", {
      method: "PUT",
      body: JSON.stringify({ model }),
    }),
  // Re-embed everything (#332): fan out to every reindexable module's /reindex so existing
  // vectors are rebuilt with the current embedding model. Returns a per-module status.
  reembed: () =>
    request(
      z.object({
        modules: z.array(z.object({ module: z.string(), status: z.string() })),
      }),
      "/platform/v1/modules/reembed",
      { method: "POST" },
    ),
  setContextWindow: (value: number | null) =>
    request(z.object({ status: z.string() }), "/platform/v1/llm/prefs/context-window", {
      method: "PUT",
      body: JSON.stringify({ value }),
    }),
  // Global Ollama KV-cache type ("q8_0"|"q4_0"|null=default f16). The core writes Ollama's env
  // file and restarts it to apply (#307); `applied` is false when Docker isn't wired, so the UI
  // falls back to the manual-restart instructions.
  setKvCacheType: (value: string | null) =>
    request(
      z.object({ status: z.string(), applied: z.boolean() }),
      "/platform/v1/llm/prefs/kv-cache-type",
      { method: "PUT", body: JSON.stringify({ value }) },
    ),
  // Agent loop bound (tool rounds per turn); null = the env default. The core clamps 1-12.
  setAgentMaxSteps: (value: number | null) =>
    request(
      z.object({ status: z.string(), value: z.number().nullable() }),
      "/platform/v1/llm/prefs/agent-max-steps",
      { method: "PUT", body: JSON.stringify({ value }) },
    ),
  setModelHidden: (name: string, hidden: boolean) =>
    request(z.object({ status: z.string(), hidden: z.array(z.string()) }), "/platform/v1/llm/prefs/hidden", {
      method: "PUT",
      body: JSON.stringify({ name, hidden }),
    }),

  // Per-model settings (context window + keep-alive). `model` is a query param —
  // names carry ":" and "/" which proxies may mangle in a path.
  modelSettings: (model: string) =>
    request(ModelSettings, `/platform/v1/llm/model-settings?model=${encodeURIComponent(model)}`),
  setModelSettings: (
    model: string,
    settings: { context_window: number | null; keep_alive: string | null; device: string | null },
  ) =>
    request(z.object({ status: z.string() }), "/platform/v1/llm/model-settings", {
      method: "PUT",
      body: JSON.stringify({ model, ...settings }),
    }),
  // A freshly pulled model should open with a context window sized to itself, not the global
  // default (#386). The core owns the heuristic (VRAM + model size + KV cache) and persists the
  // result as this model's per-model context. Best-effort and non-destructive: an existing
  // per-model override is left untouched (`applied` false), as is a model with no local size.
  suggestModelContext: (model: string) =>
    request(
      z.object({
        model: z.string(),
        context_window: z.number().nullable(),
        applied: z.boolean(),
      }),
      "/platform/v1/llm/model-settings/suggest-context",
      { method: "POST", body: JSON.stringify({ model }) },
    ),
  // Read-only facts (quantization, parameter size, trained context length) for the sheet.
  modelDetails: (model: string) =>
    request(ModelDetails, `/platform/v1/llm/models/details?model=${encodeURIComponent(model)}`),
  // The quant variants available for a model, looked up on demand from the registry (#330).
  modelVariants: (model: string) =>
    request(ModelVariants, `/platform/v1/llm/catalog/variants?model=${encodeURIComponent(model)}`),
  // Unload model(s) from memory now (keep_alive=0) without changing power state (#331).
  // `model` null/omitted unloads every loaded model.
  unloadModel: (model: string | null = null) =>
    request(z.object({ status: z.string(), model: z.string() }), "/platform/v1/llm/unload", {
      method: "POST",
      body: JSON.stringify({ model }),
    }),

  timezone: () => request(TimezonePrefs, "/platform/v1/timezone"),
  setTimezone: (timezone: string) =>
    request(z.object({ status: z.string(), timezone: z.string() }), "/platform/v1/timezone", {
      method: "PUT",
      body: JSON.stringify({ timezone }),
    }),

  providers: () => request(z.array(ProviderInfo), "/platform/v1/llm/providers"),
  setProviderKey: (alias: string, apiKey: string, apiBase?: string) =>
    request(z.object({ status: z.string() }), `/platform/v1/llm/providers/${alias}/key`, {
      method: "PUT",
      body: JSON.stringify({ api_key: apiKey, api_base: apiBase || null }),
    }),
  clearProviderKey: (alias: string) =>
    request(z.object({ status: z.string() }), `/platform/v1/llm/providers/${alias}/key`, {
      method: "DELETE",
    }),

  sessions: () => request(z.array(SessionSummary), "/platform/v1/agent/sessions"),
  sessionMessages: (id: string) =>
    request(z.array(MessageRecord), `/platform/v1/agent/sessions/${encodeURIComponent(id)}`),
  // The session's in-flight turn to re-attach to after a reload/reconnect, or null (#376).
  activeRun: (id: string) =>
    request(
      ActiveRun.nullable(),
      `/platform/v1/agent/sessions/${encodeURIComponent(id)}/active-run`,
    ),
  // Cancel the session's in-flight turn — the explicit Stop (the turn now outlives the
  // connection, so Stop must say so server-side, #376).
  cancelActiveRun: (id: string) =>
    request(
      z.object({ cancelled: z.boolean() }),
      `/platform/v1/agent/sessions/${encodeURIComponent(id)}/active-run`,
      { method: "DELETE" },
    ),
  deleteSession: (id: string) =>
    request(z.object({ deleted: z.number() }), `/platform/v1/agent/sessions/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),

  // The cross-chat memory corpus — the durable facts the model remembers about the user.
  // No `q` = the corpus newest-first; with `q` = what recall surfaces for that query.
  // `total` is the full size.
  memory: (q?: string, limit?: number) => {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (limit != null) params.set("limit", String(limit));
    const query = params.size ? `?${params}` : "";
    return request(MemoryListing, `/platform/v1/agent/memory${query}`);
  },
  // Forget one remembered fact so it stops being recalled (the conversation is kept).
  forgetMemory: (id: string) =>
    request(z.object({ forgotten: z.number() }), `/platform/v1/agent/memory/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),

  modules: () => request(z.array(ModuleSnapshot), "/platform/v1/modules"),
  moduleConfig: (name: string) =>
    request(z.record(z.string(), z.unknown()), `/platform/v1/modules/${encodeURIComponent(name)}/config`),
  saveModuleConfig: (name: string, values: Record<string, unknown>) =>
    request(z.object({ status: z.string() }), `/platform/v1/modules/${encodeURIComponent(name)}/config`, {
      method: "PUT",
      body: JSON.stringify(values),
    }),
  // Enable or disable a module (#126): hides its tools/pages/UI; the container keeps running.
  setModuleEnabled: (name: string, enabled: boolean) =>
    request(z.object({ status: z.string() }), `/platform/v1/modules/${encodeURIComponent(name)}/enabled`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),
  // Confirmed module removal (#127): stop + remove the module's container, then tombstone it.
  removeModule: (name: string) =>
    request(
      z.object({ removed: z.string(), containers: z.number() }),
      `/platform/v1/modules/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),
  // The module's per-slot model selections (#128): { slot_key: model_id }.
  getModuleModels: (name: string) =>
    request(
      z.object({ models: z.record(z.string(), z.string()) }).transform((o) => o.models),
      `/platform/v1/modules/${encodeURIComponent(name)}/models`,
    ),
  setModuleModels: (name: string, models: Record<string, string>) =>
    request(z.object({ status: z.string() }), `/platform/v1/modules/${encodeURIComponent(name)}/models`, {
      method: "PUT",
      body: JSON.stringify({ models }),
    }),
  // The module's connected accounts + collections, merged with the operator's selection
  // (ADR-0030): the shell renders per-collection toggles + an active switcher from this.
  getModuleCollections: (name: string) =>
    request(AccountsView, `/platform/v1/modules/${encodeURIComponent(name)}/collections`),
  saveModuleCollections: (name: string, prefs: CollectionPrefs) =>
    request(
      z.object({ status: z.string() }),
      `/platform/v1/modules/${encodeURIComponent(name)}/collections`,
      { method: "PUT", body: JSON.stringify(prefs) },
    ),
  // Enable or disable a single tool (#213); the module keeps running, other tools unaffected.
  setToolEnabled: (name: string, tool: string, enabled: boolean) =>
    request(
      z.object({ status: z.string() }),
      `/platform/v1/modules/${encodeURIComponent(name)}/tools/${encodeURIComponent(tool)}/enabled`,
      { method: "POST", body: JSON.stringify({ enabled }) },
    ),
  invokeModuleTool: (name: string, tool: string, args: Record<string, unknown>) =>
    request(
      z.object({ result: z.string() }),
      `/platform/v1/modules/${encodeURIComponent(name)}/tools/${encodeURIComponent(tool)}`,
      { method: "POST", body: JSON.stringify({ arguments: args }) },
    ),
  moduleStatus: (name: string) =>
    request(z.record(z.string(), z.unknown()), `/platform/v1/modules/${encodeURIComponent(name)}/status`),
  // A module page's data, proxied through the core. The shape is the page
  // archetype's contract (e.g. BrowserData); the screen validates it (ADR-0018).
  // Extra params (e.g. path, q for the storage browser) are forwarded as-is.
  modulePage: (name: string, pageId: string, params?: Record<string, string>) => {
    const query = params && Object.keys(params).length ? `?${new URLSearchParams(params)}` : "";
    return request(
      z.record(z.string(), z.unknown()),
      `/platform/v1/modules/${encodeURIComponent(name)}/pages/${encodeURIComponent(pageId)}${query}`,
    );
  },
  // One `editor` document's content, proxied through the core (ADR-0018).
  modulePageDoc: (name: string, pageId: string, path: string) =>
    request(
      EditorDocContent,
      `/platform/v1/modules/${encodeURIComponent(name)}/pages/${encodeURIComponent(pageId)}/doc?path=${encodeURIComponent(path)}`,
    ),
  // Save an `editor` document; the module writes it and (for knowledge) re-indexes it.
  saveModulePageDoc: (name: string, pageId: string, path: string, content: string) =>
    request(
      EditorSaveResult,
      `/platform/v1/modules/${encodeURIComponent(name)}/pages/${encodeURIComponent(pageId)}/doc?path=${encodeURIComponent(path)}`,
      { method: "PUT", body: JSON.stringify({ content }) },
    ),
  // An `editor` document's save history, newest first (ADR-0046).
  modulePageDocVersions: (name: string, pageId: string, path: string) =>
    request(
      EditorVersionList,
      `/platform/v1/modules/${encodeURIComponent(name)}/pages/${encodeURIComponent(pageId)}/doc/versions?path=${encodeURIComponent(path)}`,
    ),
  // One past version of an `editor` document (ADR-0046).
  modulePageDocVersion: (name: string, pageId: string, path: string, versionId: string) =>
    request(
      EditorVersionContent,
      `/platform/v1/modules/${encodeURIComponent(name)}/pages/${encodeURIComponent(pageId)}/doc/version?path=${encodeURIComponent(path)}&version=${encodeURIComponent(versionId)}`,
    ),
  // Create a folder inside an editor page's store (#216).
  createModuleFolder: async (name: string, pageId: string, path: string): Promise<void> => {
    await request(
      z.record(z.string(), z.unknown()),
      `/platform/v1/modules/${encodeURIComponent(name)}/pages/${encodeURIComponent(pageId)}/folder?path=${encodeURIComponent(path)}`,
      { method: "POST" },
    );
  },
  // Create a new knowledge base (project) — a top-level scope (#KB-refactor).
  createModuleProject: (name: string, pageId: string, projectName: string) =>
    request(
      EditorScope,
      `/platform/v1/modules/${encodeURIComponent(name)}/pages/${encodeURIComponent(pageId)}/project?project=${encodeURIComponent(projectName)}`,
      { method: "POST" },
    ),
  // Delete a knowledge base (project) and its indexed documents (#340).
  deleteModuleProject: async (name: string, pageId: string, projectName: string): Promise<void> => {
    const response = await fetch(
      `/platform/v1/modules/${encodeURIComponent(name)}/pages/${encodeURIComponent(pageId)}/project?project=${encodeURIComponent(projectName)}`,
      { method: "DELETE", headers: { "Content-Type": "application/json" } },
    );
    if (!response.ok) {
      let detail = response.statusText;
      try { detail = (await response.json()).detail ?? detail; } catch { /* non-JSON */ }
      throw new ApiError(response.status, detail);
    }
  },
  // Delete a document from an editor page's store (#216).
  deleteModuleDoc: async (name: string, pageId: string, path: string): Promise<void> => {
    const response = await fetch(
      `/platform/v1/modules/${encodeURIComponent(name)}/pages/${encodeURIComponent(pageId)}/doc?path=${encodeURIComponent(path)}`,
      { method: "DELETE", headers: { "Content-Type": "application/json" } },
    );
    if (!response.ok) {
      let detail = response.statusText;
      try { detail = (await response.json()).detail ?? detail; } catch { /* non-JSON */ }
      throw new ApiError(response.status, detail);
    }
  },
  // Delete an empty folder from an editor page's store (#216).
  deleteModuleFolder: async (name: string, pageId: string, path: string): Promise<void> => {
    const response = await fetch(
      `/platform/v1/modules/${encodeURIComponent(name)}/pages/${encodeURIComponent(pageId)}/folder?path=${encodeURIComponent(path)}`,
      { method: "DELETE", headers: { "Content-Type": "application/json" } },
    );
    if (!response.ok) {
      let detail = response.statusText;
      try { detail = (await response.json()).detail ?? detail; } catch { /* non-JSON */ }
      throw new ApiError(response.status, detail);
    }
  },
  // Move or rename a file or folder within an editor page's store (#216).
  moveModuleItem: (name: string, pageId: string, fromPath: string, toPath: string) =>
    request(
      z.object({ path: z.string() }),
      `/platform/v1/modules/${encodeURIComponent(name)}/pages/${encodeURIComponent(pageId)}/move`,
      { method: "POST", body: JSON.stringify({ from_path: fromPath, to_path: toPath }) },
    ),
  // Approve a staged suggestion on a `review` page — applies + indexes it (#220).
  // `content` (optional) is the operator's per-hunk-merged result for an edit (#KB-refactor).
  approveSuggestion: (
    name: string,
    pageId: string,
    suggestionId: string,
    content?: string,
  ) =>
    request(
      z.record(z.string(), z.unknown()),
      `/platform/v1/modules/${encodeURIComponent(name)}/pages/${encodeURIComponent(pageId)}/suggestions/${encodeURIComponent(suggestionId)}/approve`,
      { method: "POST", body: JSON.stringify(content === undefined ? {} : { content }) },
    ),
  // Reject a staged suggestion on a `review` page — discards it (#220).
  rejectSuggestion: (name: string, pageId: string, suggestionId: string) =>
    request(
      z.record(z.string(), z.unknown()),
      `/platform/v1/modules/${encodeURIComponent(name)}/pages/${encodeURIComponent(pageId)}/suggestions/${encodeURIComponent(suggestionId)}/reject`,
      { method: "POST" },
    ),
  // The cross-module pending-suggestions feed (#KB-refactor): drives the chat composer
  // bubble and the Suggestions page; each item carries its owning module + page id.
  suggestions: () => request(z.array(PendingSuggestion), `/platform/v1/suggestions`),
  // Whether a module's agent changes go through review (#KB-refactor). Off ⇒ auto-accept.
  suggestionsEnabled: (module: string) =>
    request(
      z.object({ enabled: z.boolean() }),
      `/platform/v1/modules/${encodeURIComponent(module)}/suggestions-enabled`,
    ),
  setSuggestionsEnabled: (module: string, enabled: boolean) =>
    request(
      z.record(z.string(), z.unknown()),
      `/platform/v1/modules/${encodeURIComponent(module)}/suggestions-enabled`,
      { method: "PUT", body: JSON.stringify({ enabled }) },
    ),
  // Resolve an entity reference to its hover-card envelope, proxied by the core (ADR-0019).
  resolveEntity: (name: string, kind: string, refId: string) =>
    request(
      HoverCard,
      `/platform/v1/modules/${encodeURIComponent(name)}/resolve/${encodeURIComponent(kind)}/${encodeURIComponent(refId)}`,
    ),
  // A module's attachment picker — the entities it offers to attach (ADR-0019).
  moduleAttachments: (name: string) =>
    request(
      z.array(ModuleAttachmentItem),
      `/platform/v1/modules/${encodeURIComponent(name)}/attachments`,
    ),
  // Full email message for the right-panel email-reader view (ADR-0019).
  readMailMessage: (module: string, refId: string) =>
    request(
      EmailMessage,
      `/platform/v1/modules/${encodeURIComponent(module)}/messages/${encodeURIComponent(refId)}`,
    ),
  // A text file's contents for the Files split-screen reader (#KB-refactor, req 6).
  readModuleText: (module: string, path: string) =>
    request(
      FileText,
      `/platform/v1/modules/${encodeURIComponent(module)}/read?path=${encodeURIComponent(path)}`,
    ),

  // Upload a file to attach to a chat turn; returns its core-side handle (ADR-0019).
  // Multipart, so it bypasses the JSON `request` helper.
  uploadAttachment: async (file: File): Promise<AttachmentUploaded> => {
    const form = new FormData();
    form.append("file", file);
    const response = await fetch("/platform/v1/agent/attachments", { method: "POST", body: form });
    if (!response.ok) {
      let detail = response.statusText;
      try {
        detail = (await response.json()).detail ?? detail;
      } catch {
        /* non-JSON error body */
      }
      throw new ApiError(response.status, detail);
    }
    return AttachmentUploaded.parse(await response.json());
  },

  info: () => request(PlatformInfo, "/platform/v1/info"),

  // Host system + GPU snapshot backing the Models page's context-window suggestion.
  systemInfo: () => request(SystemInfo, "/platform/v1/system/info"),

  readiness: (model?: string) => {
    const q = model ? `?model=${encodeURIComponent(model)}` : "";
    return request(Readiness, `/platform/v1/readiness${q}`);
  },

  oauthClientStatus: (provider: string) =>
    request(OAuthClientStatus, `/platform/v1/oauth/${encodeURIComponent(provider)}/client`),
  oauthSetClient: (provider: string, clientId: string, clientSecret: string) =>
    request(z.object({ status: z.string() }), `/platform/v1/oauth/${encodeURIComponent(provider)}/client`, {
      method: "PUT",
      body: JSON.stringify({ client_id: clientId, client_secret: clientSecret }),
    }),
  oauthStatus: (provider: string) =>
    request(OAuthStatus, `/platform/v1/oauth/${encodeURIComponent(provider)}/status`),
  // `scope` (space-separated) requests module API scopes beyond the default identity ones
  // (#241); the core unions them onto the defaults and accumulates prior grants.
  oauthConnect: (provider: string, scope?: string) =>
    request(
      OAuthConnectResponse,
      `/platform/v1/oauth/${encodeURIComponent(provider)}/connect${
        scope ? `?scope=${encodeURIComponent(scope)}` : ""
      }`,
    ),
  oauthDisconnect: (provider: string) =>
    request(z.object({ status: z.string() }), `/platform/v1/oauth/${encodeURIComponent(provider)}`, {
      method: "DELETE",
    }),
};

/**
 * Stream structured log entries from the core as an async generator.
 *
 * The core replays up to 200 buffered history entries first, then trickles live
 * entries as they are emitted. The caller is responsible for aborting the stream
 * via ``signal``.
 *
 * @param level    Minimum log level to receive ("debug" | "info" | "warning" | "error" | "critical").
 * @param service  Optional service-name prefix filter.
 * @param signal   AbortSignal used to stop the stream.
 */
export async function* logStream(
  level?: string,
  service?: string,
  signal?: AbortSignal,
): AsyncGenerator<LogEntry> {
  const params = new URLSearchParams();
  if (level) params.set("level", level);
  if (service) params.set("service", service);
  const query = params.size ? `?${params}` : "";
  const url = `/platform/v1/logs/stream${query}`;

  const response = await fetch(url, {
    method: "GET",
    headers: { Accept: "text/event-stream" },
    signal,
  });

  if (!response.ok || !response.body) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw Object.assign(new Error(detail), { status: response.status });
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let index: number;
      while ((index = buffer.search(/\n\n|\r\n\r\n/)) >= 0) {
        const block = buffer.slice(0, index).replace(/\r\n/g, "\n");
        buffer = buffer.slice(index + (buffer[index] === "\r" ? 4 : 2));
        const msg = parseFrame(block);
        if (msg?.event === "log") {
          try {
            yield LogEntry.parse(JSON.parse(msg.data));
          } catch {
            /* malformed frame — skip */
          }
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
