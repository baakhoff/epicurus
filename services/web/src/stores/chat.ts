/**
 * The chat stream machine. One turn at a time: send → stream deltas/tool events →
 * done|error|aborted. Completed turns belong to the server (TanStack Query refetches the
 * session); this store owns only the live exchange.
 *
 * Durability (#376): the turn runs server-side decoupled from the connection, so the store
 * persists its `sessionId` (the transcript rehydrates on reload) and, on a dropped stream /
 * reload / app-resume, **re-attaches** to the still-running turn instead of losing it. Live
 * state (segments/streaming/abort) is deliberately *not* persisted — only `sessionId`, `draft`,
 * and any pending `ask_user` question (`awaiting`), whose suspended run is durable server-side.
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";

import { api } from "@/lib/api";
import { AgentEvent, type Attachment, EmailDraft, type Readiness } from "@/lib/contracts";
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

/**
 * Why a `reattachLoop` run was entered (#477): a "probe" is an opportunistic check with no
 * prior evidence a turn is running (mount / `visibilitychange` / `online`) — the loop may
 * find nothing, and that's a perfectly normal idle chat. A "recovery" is entered already
 * knowing a turn exists (a 409 from a fresh send, or our own stream dropping mid-turn) — the
 * user has real, in-flight state to reconcile with.
 */
type ReattachMode = "probe" | "recovery";

/**
 * Decide what happens when `reattachLoop` exhausts every retry attempt without reaching a
 * terminal outcome (`done` / `gone` / a confirmed absence of a run).
 *
 * TODO(you): this is the actual reliability/UX call the rest of the loop's plumbing serves.
 * A pure probe that never found anything real should give up quietly — the user never saw a
 * turn start, so a scary "lost connection" banner would be reporting a problem that doesn't
 * exist (the #477 bug). But a probe *can* turn into a real recovery mid-flight: if it found
 * an active run (`sawActiveRun`) and then lost the stream again, the user now has a genuine
 * in-flight turn to reconcile — silently dropping that would strand them with a stale
 * spinner and no explanation.
 *
 * @param mode - how this loop was entered (see {@link ReattachMode}).
 * @param sawActiveRun - whether `api.activeRun` ever confirmed a live run during *this*
 *   loop's attempts, even if a later attempt then failed to attach to it.
 * @returns the store patch to apply. Setting `error` (+ `reconnectable: true`) surfaces the
 *   banner with an in-place Reconnect action; `error: null` gives up silently — the next
 *   mount/`visibilitychange`/`online` re-arms for free, since it's a brand-new call with its
 *   own fresh attempt budget.
 */
function classifyExhaustion(
  mode: ReattachMode,
  sawActiveRun: boolean,
): { streaming: false; abort: null; error: string | null; reconnectable: boolean } {
  // A confirmed run at any point this loop makes it a recovery from that point on, even if
  // it was entered as a probe — the user now has real in-flight state to reconcile with,
  // regardless of how we first noticed it.
  const isRecovery = mode === "recovery" || sawActiveRun;
  if (!isRecovery) {
    return { streaming: false, abort: null, error: null, reconnectable: false };
  }
  return {
    streaming: false,
    abort: null,
    error: "lost connection to the running turn",
    reconnectable: true,
  };
}

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
  /** Whether `error` can be retried in place via {@link reconnect} rather than needing a
   *  reload (#477) — true only for a reattach loop that exhausted its budget in recovery
   *  mode; any other error (a genuine stream/tool failure) leaves this false. */
  reconnectable: boolean;
  paused: boolean;
  abort: AbortController | null;
  /** The last live-run seq seen this turn — the re-attach offset (#376). Not persisted: a
   *  reload starts at 0 so the whole in-flight turn replays and rebuilds the segments. */
  lastSeq: number;
  /** A clarifying question the turn paused on (`ask_user`, ADR-0053): the suspended `runId`
   *  to resume + the `question` to put to the user. Null when nothing is pending. Persisted
   *  (unlike the rest of the live turn) so a refresh mid-question keeps the prompt — the
   *  suspended run stays durable server-side (24h). */
  awaiting: { runId: string; question: string } | null;
  /** A composed email the turn paused on for Confirm/Decline (draft-first send, ADR-0085/#563):
   *  the suspended `runId` to resolve + the `draft` to render in the split-pane. Null when nothing
   *  is pending. Persisted like `awaiting` so a reload mid-review keeps the pane and the pending
   *  draft — the suspended run stays durable server-side (24h). Mutually exclusive with `awaiting`. */
  awaitingDraft: { runId: string; draft: EmailDraft } | null;

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
   *  `visibilitychange`→visible, and `online`; a no-op when a stream is already live.
   *  `isConnectivitySignal` (#477) marks the `online` listener specifically — a signal that
   *  connectivity just returned, which resets an already-in-flight loop's attempt budget
   *  rather than merely being ignored while one is running. */
  resumeIfActive: (onDone: () => Promise<void>, isConnectivitySignal?: boolean) => Promise<void>;
  /** Retry an exhausted reattach in place after the user taps "Reconnect" (#477) — a no-op
   *  unless `reconnectable` is set. Always runs in recovery mode: the user asked for this
   *  explicitly, so a second exhaustion should surface the banner again, not go quiet. */
  reconnect: (onDone: () => Promise<void>) => Promise<void>;
  /** Answer the pending `ask_user` question (ADR-0053): POST the answer to resume the
   *  suspended run, then stream the continuation like any turn. A no-op if nothing is pending. */
  resume: (answer: string, onDone: () => Promise<void>) => Promise<void>;
  /** Confirm (`"send"`) or Decline (`"decline"`) the pending draft (ADR-0085, #563): POST the
   *  decision to resolve the suspended run, then stream the continuation like any turn. On send the
   *  core transmits the reviewed draft; on decline nothing is sent. A no-op if nothing is pending
   *  or a stream is live. `reason` is an optional short note carried back to the model on decline. */
  resolveDraft: (
    decision: "send" | "decline",
    onDone: () => Promise<void>,
    reason?: string,
  ) => Promise<void>;
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
      // Set by a fresh `online` event landing while a loop is already sleeping in backoff
      // (#477): connectivity just came back, so the in-flight loop should get a full new
      // attempt budget instead of burning through its remaining quota as if nothing changed.
      let resetRequested = false;
      // The in-flight loop's current mode (#477) — starts as whatever it was entered with,
      // but can only ever escalate probe→recovery (a confirmed run, or a fresh recovery
      // signal arriving mid-loop), never the reverse.
      let reattachMode: ReattachMode = "probe";

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
              set({ error: detail, paused: /paused/i.test(detail), reconnectable: false });
              return "error";
            } else if (event.type === "gone") return "gone";
            else if (event.type === "awaiting_input") {
              // The turn paused for the user. A `draft_review` pause (ADR-0085, #563) carries a
              // composed email to Confirm/Decline in the split-pane; anything else is an ask_user
              // clarifying question shown inline (ADR-0053). Either keeps the run durable
              // server-side, so a refresh mid-pause can still resolve it. A blank question still
              // pauses (the prompt shows a generic fallback).
              if (event.run_id && event.awaiting_kind === "draft_review")
                set({
                  awaitingDraft: {
                    runId: event.run_id,
                    draft: EmailDraft.parse(event.draft ?? {}),
                  },
                });
              else if (event.run_id)
                set({ awaiting: { runId: event.run_id, question: event.question ?? "" } });
              return "awaiting_input";
            } else if (event.type === "done") return "done";
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
            reconnectable: false,
          });
        } else if (status === "awaiting_input") {
          // Paused for a clarifying question (ask_user, ADR-0053): keep the partial turn (any
          // preamble + the ask_user step) visible and stop the spinner. The pending question
          // now lives in `awaiting`; the resume UI answers it and continues the turn. The user
          // message is already in history, so drop the optimistic echo — but keep `segments`.
          await onDone();
          set({
            streaming: false,
            abort: null,
            pendingUser: null,
            pendingAttachments: [],
            readiness: null,
            reconnectable: false,
          });
        } else {
          // "error" (detail already set) or "aborted" (user stop): keep the partial answer.
          set({ streaming: false, abort: null });
        }
      };

      // The turn is running server-side but our stream dropped (or we just reloaded, or
      // we're merely checking in): find the live run for this session and re-attach,
      // replaying from `lastSeq`. Retries with backoff; falls back to history if the run
      // finished while we were away. `mode` (#477) governs only what happens if every
      // attempt fails — see `classifyExhaustion`.
      const reattachLoop = async (
        onDone: () => Promise<void>,
        mode: ReattachMode,
        isConnectivitySignal = false,
      ): Promise<void> => {
        if (reattaching) {
          // A loop is already in flight (most likely sleeping in backoff). A confirmed
          // recovery always upgrades the running loop's mode. A connectivity signal
          // specifically (`online` — not every `visibilitychange`) means the network may
          // have just returned, so ask the loop to reset its attempt budget rather than
          // burn through its remaining quota as if nothing changed.
          if (mode === "recovery") reattachMode = "recovery";
          if (isConnectivitySignal) resetRequested = true;
          return;
        }
        reattaching = true;
        reattachMode = mode;
        let sawActiveRun = false;
        const sessionId = get().sessionId;
        try {
          let attempt = 0;
          while (attempt < MAX_REATTACH_ATTEMPTS) {
            if (resetRequested) {
              resetRequested = false;
              attempt = 0; // a connectivity signal arrived mid-loop — full budget again
            }
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
                  reconnectable: false,
                });
              }
              return;
            }
            if (active) {
              sawActiveRun = true;
              reattachMode = "recovery"; // confirmed a real turn — exhaustion now matters
              const abort = new AbortController();
              // Keep `segments` — re-attach continues the turn from `lastSeq`, doesn't restart.
              set({ streaming: true, abort, error: null, paused: false, reconnectable: false });
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
            attempt++;
          }
          set(classifyExhaustion(reattachMode, sawActiveRun));
        } finally {
          reattaching = false;
          reattachMode = "probe";
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
          awaiting: null,
          awaitingDraft: null,
          streaming: true,
          readiness: null,
          error: null,
          reconnectable: false,
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
            await reattachLoop(onDone, "recovery");
            return;
          }
          const detail = err instanceof Error ? err.message : "the request failed";
          set({
            streaming: false,
            abort: null,
            error: detail,
            reconnectable: false,
            paused: httpStatus === 503 || /paused/i.test(detail),
          });
          return;
        }
        if (status === "dropped") {
          await reattachLoop(onDone, "recovery");
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
        reconnectable: false,
        paused: false,
        abort: null,
        lastSeq: 0,
        awaiting: null,
        awaitingDraft: null,

        setDraft: (text) => set({ draft: text }),

        newSession: () => {
          get().abort?.abort();
          set({
            sessionId: freshId(),
            awaiting: null,
            awaitingDraft: null,
            pendingUser: null,
            pendingAttachments: [],
            segments: [],
            streaming: false,
            readiness: null,
            error: null,
            reconnectable: false,
            paused: false,
            abort: null,
            lastSeq: 0,
          });
        },

        openSession: (id) => {
          get().abort?.abort();
          set({
            sessionId: id,
            awaiting: null,
            awaitingDraft: null,
            pendingUser: null,
            pendingAttachments: [],
            segments: [],
            streaming: false,
            readiness: null,
            error: null,
            reconnectable: false,
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

        resume: async (answer, onDone) => {
          const awaiting = get().awaiting;
          if (awaiting === null || get().streaming) return;
          set({ awaiting: null });
          // Continue the suspended turn: POST the answer (the core appends it as the ask_user
          // tool result) and stream the continuation over the same SSE protocol — so reuse
          // runTurn. On `done` the now-complete turn refetches into history (ADR-0053).
          await runTurn(
            `/platform/v1/agent/runs/${encodeURIComponent(awaiting.runId)}/resume`,
            { answer },
            onDone,
          );
        },

        resolveDraft: async (decision, onDone, reason) => {
          const awaitingDraft = get().awaitingDraft;
          if (awaitingDraft === null || get().streaming) return;
          set({ awaitingDraft: null });
          // Continue the suspended turn: POST the decision. On `send` the core transmits the
          // reviewed draft via the module's /send and appends the outcome; on `decline` nothing is
          // sent (the model is told, with any reason). Reuse runTurn so the continuation streams +
          // re-attaches like any turn (ADR-0085). A trimmed reason is omitted rather than sent blank.
          const trimmed = reason?.trim();
          await runTurn(
            `/platform/v1/agent/runs/${encodeURIComponent(awaitingDraft.runId)}/draft`,
            trimmed ? { decision, reason: trimmed } : { decision },
            onDone,
          );
        },

        resumeIfActive: async (onDone, isConnectivitySignal) => {
          const abort = get().abort;
          // A live stream is already running (a fresh send) — don't open a competing one.
          if (get().streaming && abort && !abort.signal.aborted && !reattaching) return;
          await reattachLoop(onDone, "probe", isConnectivitySignal);
        },

        reconnect: async (onDone) => {
          if (!get().reconnectable) return;
          // The user explicitly asked for this — unlike an automatic probe, a second
          // exhaustion should surface the banner again rather than go quiet.
          await reattachLoop(onDone, "recovery");
        },

        stop: () => {
          get().abort?.abort();
          // The turn is decoupled from the connection now (#376), so aborting our stream no
          // longer ends it — tell the server to cancel, or it keeps running and blocks the next
          // send. Best-effort; if it fails the turn simply completes and lands in history.
          void api.cancelActiveRun(get().sessionId).catch(() => undefined);
        },
        clearError: () => set({ error: null, paused: false, reconnectable: false }),
      };
    },
    {
      name: "epicurus-chat",
      // Identity + draft + any pending clarifying question survive a reload; the rest of the
      // live turn is reconstructed by re-attach. The suspended run behind `awaiting` stays
      // durable server-side (24h), so a refresh mid-question can still answer it (ADR-0053).
      partialize: (state) => ({
        sessionId: state.sessionId,
        draft: state.draft,
        awaiting: state.awaiting,
        awaitingDraft: state.awaitingDraft,
      }),
    },
  ),
);
