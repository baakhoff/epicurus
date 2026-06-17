/**
 * The core API client. Same-origin: nginx (or the Vite dev proxy) forwards
 * /platform/* to the core container, so there is no CORS anywhere.
 */
import { z } from "zod";

import {
  AttachmentUploaded,
  EditorDocContent,
  EditorSaveResult,
  EmailMessage,
  HoverCard,
  LlmPrefs,
  MessageRecord,
  ModelInfo,
  ModuleAttachmentItem,
  ModuleSnapshot,
  OAuthClientStatus,
  OAuthConnectResponse,
  OAuthStatus,
  PlatformInfo,
  PowerStatus,
  ProviderInfo,
  SessionSummary,
  type PowerState,
} from "@/lib/contracts";

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

  models: () => request(z.array(ModelInfo), "/platform/v1/llm/models"),
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
  setModelHidden: (name: string, hidden: boolean) =>
    request(z.object({ status: z.string(), hidden: z.array(z.string()) }), "/platform/v1/llm/prefs/hidden", {
      method: "PUT",
      body: JSON.stringify({ name, hidden }),
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
  deleteSession: (id: string) =>
    request(z.object({ deleted: z.number() }), `/platform/v1/agent/sessions/${encodeURIComponent(id)}`, {
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

  oauthClientStatus: (provider: string) =>
    request(OAuthClientStatus, `/platform/v1/oauth/${encodeURIComponent(provider)}/client`),
  oauthSetClient: (provider: string, clientId: string, clientSecret: string) =>
    request(z.object({ status: z.string() }), `/platform/v1/oauth/${encodeURIComponent(provider)}/client`, {
      method: "PUT",
      body: JSON.stringify({ client_id: clientId, client_secret: clientSecret }),
    }),
  oauthStatus: (provider: string) =>
    request(OAuthStatus, `/platform/v1/oauth/${encodeURIComponent(provider)}/status`),
  oauthConnect: (provider: string) =>
    request(OAuthConnectResponse, `/platform/v1/oauth/${encodeURIComponent(provider)}/connect`),
  oauthDisconnect: (provider: string) =>
    request(z.object({ status: z.string() }), `/platform/v1/oauth/${encodeURIComponent(provider)}`, {
      method: "DELETE",
    }),
};
