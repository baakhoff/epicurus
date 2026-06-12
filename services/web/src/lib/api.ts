/**
 * The core API client. Same-origin: nginx (or the Vite dev proxy) forwards
 * /platform/* to the core container, so there is no CORS anywhere.
 */
import { z } from "zod";

import {
  MessageRecord,
  ModelInfo,
  ModuleSnapshot,
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
  invokeModuleTool: (name: string, tool: string, args: Record<string, unknown>) =>
    request(
      z.object({ result: z.string() }),
      `/platform/v1/modules/${encodeURIComponent(name)}/tools/${encodeURIComponent(tool)}`,
      { method: "POST", body: JSON.stringify({ arguments: args }) },
    ),

  info: () => request(PlatformInfo, "/platform/v1/info"),
};
