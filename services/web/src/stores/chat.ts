/**
 * The chat stream machine. One turn at a time: send → stream deltas/tool
 * events → done|error|aborted. Completed turns belong to the server (TanStack
 * Query refetches the session); this store owns only the live exchange.
 */
import { create } from "zustand";

import { AgentEvent, type Attachment, type Readiness } from "@/lib/contracts";
import { sse } from "@/lib/sse";

export interface ToolRun {
  tool: string;
  status: "running" | "ok" | "error";
  detail?: string;
}

export type ChatSegment =
  | { kind: "text"; text: string }
  | { kind: "tool"; run: ToolRun };

interface ChatState {
  sessionId: string;
  /** Unsent composer text. Lives here (not in the screen) so it survives leaving
   *  and returning to the chat page; cleared on send. */
  draft: string;
  /** The user message currently being answered (optimistic echo). */
  pendingUser: string | null;
  /** The assistant turn under construction, in order. */
  segments: ChatSegment[];
  streaming: boolean;
  /** Warming progress emitted before the first token (ADR-0027); null once answered. */
  readiness: Readiness | null;
  error: string | null;
  paused: boolean;
  abort: AbortController | null;

  setDraft: (text: string) => void;
  newSession: () => void;
  openSession: (id: string) => void;
  /** `onDone` must complete the server-history refetch — the live turn is
   *  cleared right after it resolves, so the transcript never doubles. */
  send: (
    text: string,
    model: string | null,
    onDone: () => Promise<void>,
    attachments?: Attachment[],
  ) => Promise<void>;
  stop: () => void;
  clearError: () => void;
}

function freshId(): string {
  return crypto.randomUUID();
}

export const useChat = create<ChatState>()((set, get) => ({
  sessionId: freshId(),
  draft: "",
  pendingUser: null,
  segments: [],
  streaming: false,
  readiness: null,
  error: null,
  paused: false,
  abort: null,

  setDraft: (text) => set({ draft: text }),

  newSession: () => {
    get().abort?.abort();
    set({
      sessionId: freshId(),
      pendingUser: null,
      segments: [],
      streaming: false,
      readiness: null,
      error: null,
      paused: false,
      abort: null,
    });
  },

  openSession: (id) => {
    get().abort?.abort();
    set({
      sessionId: id,
      pendingUser: null,
      segments: [],
      streaming: false,
      readiness: null,
      error: null,
      paused: false,
      abort: null,
    });
  },

  send: async (text, model, onDone, attachments) => {
    if (get().streaming) return;
    const abort = new AbortController();
    set({
      draft: "",
      pendingUser: text,
      segments: [],
      streaming: true,
      readiness: null,
      error: null,
      paused: false,
      abort,
    });

    const push = (segment: ChatSegment) => set({ segments: [...get().segments, segment] });
    const appendText = (delta: string) => {
      const segments = [...get().segments];
      const last = segments[segments.length - 1];
      if (last?.kind === "text") {
        segments[segments.length - 1] = { kind: "text", text: last.text + delta };
        set({ segments });
      } else {
        push({ kind: "text", text: delta });
      }
    };
    const setTool = (run: ToolRun) => {
      const segments = [...get().segments];
      for (let i = segments.length - 1; i >= 0; i--) {
        const segment = segments[i];
        if (segment.kind === "tool" && segment.run.tool === run.tool && segment.run.status === "running") {
          segments[i] = { kind: "tool", run };
          set({ segments });
          return;
        }
      }
      push({ kind: "tool", run });
    };

    let completed = false;
    try {
      const body = {
        messages: [
          {
            role: "user",
            content: text,
            attachments: attachments && attachments.length > 0 ? attachments : undefined,
          },
        ],
        model: model ?? undefined,
        session_id: get().sessionId,
      };
      for await (const message of sse("/platform/v1/agent/chat/stream", body, abort.signal)) {
        const event = AgentEvent.parse(JSON.parse(message.data));
        if (event.type === "readiness" && event.readiness) set({ readiness: event.readiness });
        else if (event.type === "delta" && event.text) appendText(event.text);
        else if (event.type === "tool" && event.tool && event.status)
          setTool({ tool: event.tool, status: event.status, detail: event.detail ?? undefined });
        else if (event.type === "error") {
          const detail = event.detail ?? "the stream failed";
          set({ error: detail, paused: /paused/i.test(detail) });
        } else if (event.type === "done") {
          completed = true;
        }
      }
      if (completed) {
        // The server now owns this turn: refetch history, then drop the live copy.
        await onDone();
        set({ streaming: false, abort: null, pendingUser: null, segments: [], readiness: null });
      } else {
        set({ streaming: false, abort: null });
      }
    } catch (err) {
      if (abort.signal.aborted) {
        // user stop: keep what streamed, the partial answer simply isn't persisted
        set({ streaming: false, abort: null });
        return;
      }
      const status = (err as { status?: number }).status;
      const detail = err instanceof Error ? err.message : "the request failed";
      set({
        streaming: false,
        abort: null,
        error: detail,
        paused: status === 503 || /paused/i.test(detail),
      });
    }
  },

  stop: () => get().abort?.abort(),
  clearError: () => set({ error: null, paused: false }),
}));
