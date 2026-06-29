/**
 * The chat stream machine. One turn at a time: send → stream deltas/tool events →
 * done|error|aborted. Completed turns belong to the server (TanStack Query refetches the
 * session); this store owns only the live exchange.
 *
 * Durability (#376): the turn runs server-side decoupled from the connection, so the store
 * persists its `sessionId` (the transcript rehydrates on reload) and, on a dropped stream /
 * reload / app-resume, **re-attaches** to the still-running turn instead of losing it. Live
 * state (segments/streaming/abort) is deliberately *not* persisted — only `sessionId`+`draft`.
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";

import { api } from "@/lib/api";
import { AgentEvent, type Attachment, type Readiness } from "@/lib/contracts";
import { sse, sseRequest, type SseMessage } from "@/lib/sse";

export interface ToolRun {
  tool: string;
  status: "running" | "ok" | "error";
  detail?: string;
}

export type ChatSegment =
  | { kind: "text"; text: string }
  | { kind: "tool"; run: ToolRun }
  | { kind: "thinking"; text: string };

/** One entry on the activity timeline (the turn's *process*): a run of thinking or a tool
 *  step, in chronological order (#300). Built from the live `segments` or a message's
 *  persisted `activity` so {@link ProcessTimeline} renders both identically. */
export type ActivityItem =
  | { kind: "thinking"; text: string }
  | { kind: "tool"; run: ToolRun };

/** How a single SSE stream ended (one turn may span several, across re-attaches). */
type StreamEnd = "done" | "error" | "gone" | "awaiting_input" | "dropped" | "aborted";

// Re-attach backoff: a dropped turn is still running server-side, so retry a few times with
// growing delay before giving up (the answer is durable regardless — history is the fallback).
const MAX_REATTACH_ATTEMPTS = 6;
const backoffMs = (attempt: number): number => Math.min(500 * 2 ** attempt, 8_000);
const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

interface ChatState {
  sessionId: string;
  /** Unsent composer text. Persisted so it survives a reload, not just navigation. */
  draft: string;
  /** The user message currently being answered (optimistic echo). */
  pendingUser: string | null;
  /** Attachments staged with that optimistic message, shown as pills beside it until the
   *  server-stored turn (which carries its own copy) takes over. Cleared with `pendingUser`. */
  pendingAttachments: Attachment[];
  /** The assistant turn under construction, in order — text (answer), tool steps, and
   *  thinking blocks interleaved exactly as they streamed (#300). The activity timeline is
   *  derived from the thinking + tool segments; cleared on `done` when the server-stored turn
   *  (which carries its own persisted activity) takes over. */
  segments: ChatSegment[];
  streaming: boolean;
  /** Warming progress emitted before the first token (ADR-0027); null once answered. */
  readiness: Readiness | null;
  error: string | null;
  paused: boolean;
  abort: AbortController | null;
  /** The last live-run seq seen this turn — the re-attach offset (#376). Not persisted: a
   *  reload starts at 0 so the whole in-flight turn replays and rebuilds the segments. */
  lastSeq: number;

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
  /** Re-answer the session's last user turn, dropping the previous answer (#302). The
   *  caller drops the stale answer from the displayed transcript before this streams. */
  regenerate: (model: string | null, onDone: () => Promise<void>) => Promise<void>;
  /** Replace the last user message with `content` and re-answer it in place (#302). */
  editAndRerun: (
    content: string,
    model: string | null,
    onDone: () => Promise<void>,
  ) => Promise<void>;
  /** Re-attach to this session's in-flight turn if one exists (#376). Called on mount,
   *  `visibilitychange`→visible, and `online`; a no-op when a stream is already live. */
  resumeIfActive: (onDone: () => Promise<void>) => Promise<void>;
  stop: () => void;
  clearError: () => void;
}

function freshId(): string {
  return crypto.randomUUID();
}

export const useChat = create<ChatState>()(
  persist(
    (set, get) => {
      // Guards a single re-attach loop at a time (it spans awaits; a second trigger — a
      // visibilitychange landing mid-reconnect — must not open a competing stream).
      let reattaching = false;

      const push = (segment: ChatSegment): void => {
        set({ segments: [...get().segments, segment] });
      };
      const appendText = (delta: string): void => {
        const segments = [...get().segments];
        const last = segments[segments.length - 1];
        if (last?.kind === "text") {
          segments[segments.length - 1] = { kind: "text", text: last.text + delta };
          set({ segments });
        } else {
          push({ kind: "text", text: delta });
        }
      };
      // Coalesce consecutive reasoning into the trailing thinking segment; a tool (or answer
      // text) between two runs of thinking splits them, so `segments` keeps the true order.
      const appendThinking = (delta: string): void => {
        const segments = [...get().segments];
        const last = segments[segments.length - 1];
        if (last?.kind === "thinking") {
          segments[segments.length - 1] = { kind: "thinking", text: last.text + delta };
          set({ segments });
        } else {
          push({ kind: "thinking", text: delta });
        }
      };
      const setTool = (run: ToolRun): void => {
        const segments = [...get().segments];
        for (let i = segments.length - 1; i >= 0; i--) {
          const segment = segments[i];
          if (
            segment.kind === "tool" &&
            segment.run.tool === run.tool &&
            segment.run.status === "running"
          ) {
            segments[i] = { kind: "tool", run };
            set({ segments });
            return;
          }
        }
        push({ kind: "tool", run });
      };

      // Consume one SSE stream into the live segments; report how it ended. Re-throws only a
      // non-OK *HTTP* error (the stream never began) so the caller can branch (409 → re-attach,
      // 503 → paused); a mid-stream network failure is reported as "dropped" (re-attachable).
      const consume = async (
        stream: AsyncGenerator<SseMessage>,
        abort: AbortController,
      ): Promise<StreamEnd> => {
        try {
          for await (const message of stream) {
            if (message.id) set({ lastSeq: Number(message.id) });
            const event = AgentEvent.parse(JSON.parse(message.data));
            if (event.type === "readiness" && event.readiness) set({ readiness: event.readiness });
            else if (event.type === "delta" && event.text) appendText(event.text);
            else if (event.type === "thinking" && event.text) appendThinking(event.text);
            else if (event.type === "tool" && event.tool && event.status)
              setTool({ tool: event.tool, status: event.status, detail: event.detail ?? undefined });
            else if (event.type === "error") {
              const detail = event.detail ?? "the stream failed";
              set({ error: detail, paused: /paused/i.test(detail) });
              return "error";
            } else if (event.type === "gone") return "gone";
            else if (event.type === "awaiting_input") return "awaiting_input";
            else if (event.type === "done") return "done";
          }
          return "dropped"; // ended without a terminal frame → the connection was lost
        } catch (err) {
          if (abort.signal.aborted) return "aborted";
          if (typeof (err as { status?: number }).status === "number") throw err; // HTTP error
          return "dropped"; // network/stream failure mid-turn — the turn runs on server-side
        }
      };

      // A stream reached a real end (never "dropped"): reconcile the store with it.
      const finishTerminal = async (
        status: Exclude<StreamEnd, "dropped">,
        onDone: () => Promise<void>,
      ): Promise<void> => {
        if (status === "done" || status === "gone") {
          // The server owns this turn now: refetch history, then drop the live copy (the
          // stored turn carries its own persisted activity). `gone` means it finished while
          // we were away and was reaped — the answer is in history just the same.
          await onDone();
          set({
            streaming: false,
            abort: null,
            pendingUser: null,
            pendingAttachments: [],
            segments: [],
            readiness: null,
            lastSeq: 0,
          });
        } else if (status === "awaiting_input") {
          // The turn paused for a clarifying question (ADR-0053). Stop the spinner and let the
          // refetched history show it; the full resume UI is #360.
          await onDone();
          set({ streaming: false, abort: null, readiness: null });
        } else {
          // "error" (detail already set) or "aborted" (user stop): keep the partial answer.
          set({ streaming: false, abort: null });
        }
      };

      // The turn is running server-side but our stream dropped (or we just reloaded): find the
      // live run for this session and re-attach, replaying from `lastSeq`. Retries with backoff;
      // falls back to history if the run finished while we were away.
      const reattachLoop = async (onDone: () => Promise<void>): Promise<void> => {
        if (reattaching) return;
        reattaching = true;
        const sessionId = get().sessionId;
        try {
          for (let attempt = 0; attempt < MAX_REATTACH_ATTEMPTS; attempt++) {
            if (get().sessionId !== sessionId) return; // switched session — abandon
            if (get().abort?.signal.aborted) return; // user stopped
            let active;
            try {
              active = await api.activeRun(sessionId);
            } catch {
              active = undefined; // server still unreachable — back off and retry
            }
            if (get().sessionId !== sessionId) return;
            if (active === null) {
              // No live run: it finished while we were away (or there never was one). Only
              // reconcile if we thought we were mid-turn; otherwise leave history untouched.
              if (get().streaming) {
                await onDone();
                set({
                  streaming: false,
                  abort: null,
                  pendingUser: null,
                  pendingAttachments: [],
                  segments: [],
                  readiness: null,
                  lastSeq: 0,
                });
              }
              return;
            }
            if (active) {
              const abort = new AbortController();
              // Keep `segments` — re-attach continues the turn from `lastSeq`, doesn't restart.
              set({ streaming: true, abort, error: null, paused: false });
              let status: StreamEnd;
              try {
                status = await consume(
                  sseRequest(
                    `/platform/v1/agent/runs/${encodeURIComponent(active.run_id)}/stream` +
                      `?after_seq=${get().lastSeq}`,
                    { method: "GET", signal: abort.signal },
                  ),
                  abort,
                );
              } catch {
                status = "dropped"; // any late error on re-attach → retry
              }
              if (get().sessionId !== sessionId) return;
              if (status !== "dropped") {
                await finishTerminal(status, onDone);
                return;
              }
            }
            await sleep(backoffMs(attempt));
          }
          set({
            streaming: false,
            abort: null,
            error: "lost connection to the running turn — reload to see the result",
          });
        } finally {
          reattaching = false;
        }
      };

      // The shared streaming core: open the SSE turn at `path` with `body`, stream it, and on a
      // clean end refetch history; on a drop, re-attach to the still-running server turn (#376).
      const runTurn = async (
        path: string,
        body: Record<string, unknown>,
        onDone: () => Promise<void>,
      ): Promise<void> => {
        const abort = new AbortController();
        set({
          segments: [],
          streaming: true,
          readiness: null,
          error: null,
          paused: false,
          abort,
          lastSeq: 0,
        });
        let status: StreamEnd;
        try {
          status = await consume(sse(path, body, abort.signal), abort);
        } catch (err) {
          const httpStatus = (err as { status?: number }).status;
          if (httpStatus === 409) {
            // A turn is already running for this session — attach to it, don't error.
            await reattachLoop(onDone);
            return;
          }
          const detail = err instanceof Error ? err.message : "the request failed";
          set({
            streaming: false,
            abort: null,
            error: detail,
            paused: httpStatus === 503 || /paused/i.test(detail),
          });
          return;
        }
        if (status === "dropped") {
          await reattachLoop(onDone);
          return;
        }
        await finishTerminal(status, onDone);
      };

      return {
        sessionId: freshId(),
        draft: "",
        pendingUser: null,
        pendingAttachments: [],
        segments: [],
        streaming: false,
        readiness: null,
        error: null,
        paused: false,
        abort: null,
        lastSeq: 0,

        setDraft: (text) => set({ draft: text }),

        newSession: () => {
          get().abort?.abort();
          set({
            sessionId: freshId(),
            pendingUser: null,
            pendingAttachments: [],
            segments: [],
            streaming: false,
            readiness: null,
            error: null,
            paused: false,
            abort: null,
            lastSeq: 0,
          });
        },

        openSession: (id) => {
          get().abort?.abort();
          set({
            sessionId: id,
            pendingUser: null,
            pendingAttachments: [],
            segments: [],
            streaming: false,
            readiness: null,
            error: null,
            paused: false,
            abort: null,
            lastSeq: 0,
          });
        },

        send: async (text, model, onDone, attachments) => {
          if (get().streaming) return;
          set({ draft: "", pendingUser: text, pendingAttachments: attachments ?? [] });
          await runTurn(
            "/platform/v1/agent/chat/stream",
            {
              messages: [
                {
                  role: "user",
                  content: text,
                  attachments: attachments && attachments.length > 0 ? attachments : undefined,
                },
              ],
              model: model ?? undefined,
              session_id: get().sessionId,
            },
            onDone,
          );
        },

        regenerate: async (model, onDone) => {
          if (get().streaming) return;
          // No optimistic user echo — the user message is unchanged; the caller has already
          // dropped the stale answer from the displayed transcript.
          set({ pendingUser: null, pendingAttachments: [] });
          const sid = encodeURIComponent(get().sessionId);
          await runTurn(
            `/platform/v1/agent/sessions/${sid}/regenerate`,
            { model: model ?? undefined },
            onDone,
          );
        },

        editAndRerun: async (content, model, onDone) => {
          if (get().streaming) return;
          set({ pendingUser: null, pendingAttachments: [] });
          const sid = encodeURIComponent(get().sessionId);
          await runTurn(
            `/platform/v1/agent/sessions/${sid}/edit`,
            { content, model: model ?? undefined },
            onDone,
          );
        },

        resumeIfActive: async (onDone) => {
          const abort = get().abort;
          // A live stream is already running (a fresh send) — don't open a competing one.
          if (get().streaming && abort && !abort.signal.aborted && !reattaching) return;
          await reattachLoop(onDone);
        },

        stop: () => {
          get().abort?.abort();
          // The turn is decoupled from the connection now (#376), so aborting our stream no
          // longer ends it — tell the server to cancel, or it keeps running and blocks the next
          // send. Best-effort; if it fails the turn simply completes and lands in history.
          void api.cancelActiveRun(get().sessionId).catch(() => undefined);
        },
        clearError: () => set({ error: null, paused: false }),
      };
    },
    {
      name: "epicurus-chat",
      // Only identity + draft survive a reload; live turn state is reconstructed by re-attach.
      partialize: (state) => ({ sessionId: state.sessionId, draft: state.draft }),
    },
  ),
);
