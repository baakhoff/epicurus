/**
 * The chat stream machine. One turn at a time: send → stream deltas/tool events →
 * done|error|aborted. Completed turns belong to the server (TanStack Query refetches the
 * session); this store owns only the live exchange.
 *
 * Durability (#376): the turn runs server-side decoupled from the connection, so the store
 * persists its `sessionId` (the transcript rehydrates on reload) and, on a dropped stream /
 * reload / app-resume, **re-attaches** to the still-running turn instead of losing it. Live
 * state (segments/streaming/abort) is deliberately *not* persisted — only `sessionId`, `draft`,
 * and any pending pause (`awaiting`/`awaitingDraft`/`awaitingApproval`), whose suspended run is
 * durable server-side.
 */
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { create } from "zustand";
import { persist } from "zustand/middleware";

import { api } from "@/lib/api";
import {
  AgentEvent,
  type Attachment,
  type DocumentPreview,
  EmailDraft,
  type EntityRef,
  type Readiness,
  type WrittenDocument,
} from "@/lib/contracts";
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

/** A document an annotated tool call is writing, as the pane sees it (#541, ADR-0101).
 *
 *  `writing` is true while the call is in flight — the pane stays read-only until the write
 *  settles, so a user edit can't race the agent's own. `failed` means the call errored and
 *  nothing was written. `tool` names the call, so the pane can be re-opened from its chip. */
export interface LiveDocument extends WrittenDocument {
  tool: string;
  writing: boolean;
  failed: boolean;
  /** The user closed the pane; a further write to the same document must not reopen it. */
  dismissed: boolean;
  /** The typewriter is running (#654, ADR-0121): `content` is what the model has typed *so far*,
   *  growing frame by frame, and the call has not been made yet. It flips false the moment the
   *  `tool` frame lands — that frame carries the arguments as actually parsed, and replaces this
   *  body wholesale, so the pane never keeps showing a guess after the real thing is known.
   *  Read-only either way: `writing` stays true from the first preview through the write. */
  streaming: boolean;
}

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
  /** The open chat is **invisible** (#772): it works normally but is hidden from the
   *  conversations list and from every learner server-side, and it is **deleted — not
   *  archived — when you leave** (toggle off, switch/new session, or close the app).
   *  Persisted (with `sessionId`) so an accidental reload mid-conversation keeps the
   *  thread; {@link useInvisibleLaunchGuard} tells a same-tab reload (resume) apart from a
   *  fresh app launch (the previous invisible chat is ended). */
  invisible: boolean;
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
  /** A staged change the turn paused on for Approve/Reject (`ask_approval`, #745, ADR-0117): the
   *  suspended `runId` to resolve + the `summary` and entity `refs` (possibly empty) to render
   *  as an approval card. Null when nothing is pending. Persisted like `awaiting`/`awaitingDraft`
   *  so a reload mid-review keeps the card — the suspended run stays durable server-side (24h).
   *  Mutually exclusive with both. */
  awaitingApproval: { runId: string; summary: string; refs: EntityRef[] } | null;
  /** The document the turn is writing, for the pane beside the chat (#541, ADR-0101). Set from
   *  a `doc_preview` event while the model types it (#654, ADR-0121) and then from the `tool`
   *  event that carries the call as parsed; null when this turn writes none. Not persisted: the
   *  body rides the SSE stream and never reaches the transcript (ADR-0041's caps are unchanged),
   *  so a reload has nothing to restore it from — the turn's entity-ref chip is the durable way
   *  back to the document (ADR-0019), and an in-flight turn re-attaches and replays the deltas.
   *  `dismissed` survives further writes to the same document, so closing the pane stays closed —
   *  including closing it mid-typewriter. */
  liveDocument: LiveDocument | null;
  /** Close the document pane. It reopens only when a *new* document is written (#541). */
  dismissDocument: () => void;
  /** Sessions that finished a turn while this wasn't the open one (#492) — a background turn
   *  the operator hasn't seen the answer to yet. One boolean marker per session (no counts,
   *  no push notifications); cleared the moment the session opens. Not persisted: a reload
   *  has no way to know what happened while the tab was closed, and {@link useAwayFinishedWatch}
   *  re-establishes ground truth from the server within one poll anyway. */
  unseenFinished: Set<string>;

  setDraft: (text: string) => void;
  newSession: () => void;
  openSession: (id: string) => void;
  /** Toggle invisible mode (#772). On: starts a **fresh** invisible session (never converts
   *  the open one — its history would already have been learned from) and flags it
   *  server-side. Off: an exit — the invisible chat is deleted and a fresh normal session
   *  starts. */
  toggleInvisible: () => void;
  /** Marks a session as having finished while unseen (#492) — called only by
   *  {@link useAwayFinishedWatch}'s poll diff, never directly by UI code. */
  markUnseenFinished: (id: string) => void;
  /** Drops a session's away-finished marker regardless of which session is current — called when
   *  a session is removed, so deleting an unseen-finished session can't strand a permanent
   *  indicator ({@link openSession} only clears the session being opened). */
  clearUnseenFinished: (id: string) => void;
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
  /** Replace a user message with `content` and re-answer it in place (#302, #552).
   *  `messageId` names the turn to revise; omit it for the last user message. Editing further
   *  back also discards the turns after it — the caller confirms that first, and trims the
   *  displayed transcript to match before this streams. */
  editAndRerun: (
    content: string,
    model: string | null,
    onDone: () => Promise<void>,
    messageId?: number,
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
  /** Approve or Reject the pending staged change (`ask_approval`, #745, ADR-0117): first call
   *  each ref's owning module's own review-decision endpoint directly (the agent never approves
   *  its own work — only the operator's own click here does), then POST the outcome to resolve
   *  the suspended run and stream the continuation like any turn. A module call failure doesn't
   *  block the resume — it is reported to the model as part of the outcome, same as a failed
   *  draft send. A no-op if nothing is pending or a stream is live. */
  resolveApproval: (
    decision: "approved" | "rejected",
    onDone: () => Promise<void>,
    comment?: string,
  ) => Promise<void>;
  stop: () => void;
  clearError: () => void;
}

function freshId(): string {
  return crypto.randomUUID();
}

/** Session-storage marker telling a same-tab **reload** (marker survives → resume the
 *  invisible chat) apart from a fresh **launch** (marker gone → the previous app-session's
 *  invisible chat is ended). Per-tab by nature; a PWA backgrounded keeps its tab — and the
 *  marker — alive, so backgrounding never ends an invisible chat (#772). */
const INVISIBLE_LIVE_MARKER = "epicurus-invisible-live";

/** Best-effort server delete of an invisible chat on exit (#772): the full #771 cascade.
 *  Failure is tolerable — the server's orphan sweep erases any flagged session this client
 *  stops naming as its live one. */
function deleteInvisible(sessionId: string): void {
  void api.deleteSession(sessionId).catch(() => undefined);
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
      // Track the document a write tool is producing, for the pane (#541). The `running` frame
      // opens it and the terminal frame settles it. A dismissal sticks while the *same*
      // document keeps being written — the user said no to this pane, and the agent finishing
      // the write it already had open isn't a reason to overrule them; a write to a different
      // document is a new event and opens afresh.
      const setLiveDocument = (
        tool: string,
        status: "running" | "ok" | "error",
        document: WrittenDocument,
      ): void => {
        const open = get().liveDocument;
        const same =
          open?.module === document.module &&
          // A typewriter that never got as far as the target still belongs to this write — the
          // preview and the call it previewed are the same document, so a mid-write dismissal
          // must survive the settle rather than being overruled by the pane reopening.
          (open?.streaming || open?.target === document.target);
        set({
          liveDocument: {
            ...document,
            tool,
            writing: status === "running",
            failed: status === "error",
            dismissed: same ? open.dismissed : false,
            // The authoritative frame: whatever the typewriter drew is replaced by the arguments
            // as actually parsed (ADR-0101 — the pane must never keep showing a guess).
            streaming: false,
          },
        });
      };

      // The typewriter (#654, ADR-0121): a `doc_preview` delta extends the body being typed.
      // Deltas are append-only and ordered, so this is a concatenation — which is also what
      // makes re-attach free: replaying the run's buffer replays these in order and rebuilds
      // exactly the same body. A frame carries `module` always and `target`/`title` once the
      // model has finished typing them, so the header fills in as it goes.
      const appendPreview = (tool: string, text: string, preview: DocumentPreview): void => {
        const open = get().liveDocument;
        // Continue the open preview when it is the same module's write; anything else (a settled
        // document, or another module) starts a fresh pane.
        const same = open !== null && open.streaming && open.module === preview.module;
        set({
          liveDocument: {
            module: preview.module,
            content: same ? open.content + text : text,
            target: preview.target ?? (same ? open.target : null),
            title: preview.title ?? (same ? open.title : null),
            tool,
            writing: true, // read-only from the very first character (#541's structural fix)
            failed: false,
            dismissed: same ? open.dismissed : false,
            streaming: true,
          },
        });
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
            else if (event.type === "tool" && event.tool && event.status) {
              setTool({ tool: event.tool, status: event.status, detail: event.detail ?? undefined });
              if (event.document) setLiveDocument(event.tool, event.status, event.document);
            } else if (event.type === "doc_preview" && event.tool && event.preview) {
              // Ephemeral by contract: nothing about a preview is persisted, and it leaves no
              // trace in `segments` — it only feeds the pane (ADR-0041/0121).
              appendPreview(event.tool, event.text ?? "", event.preview);
            }
            else if (event.type === "error") {
              const detail = event.detail ?? "the stream failed";
              set({ error: detail, paused: /paused/i.test(detail), reconnectable: false });
              return "error";
            } else if (event.type === "gone") return "gone";
            else if (event.type === "awaiting_input") {
              // The turn paused for the user. A `draft_review` pause (ADR-0085, #563) carries a
              // composed email to Confirm/Decline in the split-pane; an `approval` pause (#745,
              // ADR-0117) carries a summary + entity refs to Approve/Reject inline; anything else
              // is an ask_user clarifying question shown inline (ADR-0053). Every kind keeps the
              // run durable server-side, so a refresh mid-pause can still resolve it. A blank
              // question/summary still pauses (the prompt shows a generic fallback).
              if (event.run_id && event.awaiting_kind === "draft_review")
                set({
                  awaitingDraft: {
                    runId: event.run_id,
                    draft: EmailDraft.parse(event.draft ?? {}),
                  },
                });
              else if (event.run_id && event.awaiting_kind === "approval")
                set({
                  awaitingApproval: {
                    runId: event.run_id,
                    summary: event.summary ?? "",
                    refs: event.refs ?? [],
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
          awaitingApproval: null,
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
        invisible: false,
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
        awaitingApproval: null,
        liveDocument: null,
        unseenFinished: new Set(),

        setDraft: (text) => set({ draft: text }),

        dismissDocument: () =>
          set((s) => (s.liveDocument ? { liveDocument: { ...s.liveDocument, dismissed: true } } : s)),

        markUnseenFinished: (id) =>
          set((s) => (s.unseenFinished.has(id) ? s : { unseenFinished: new Set(s.unseenFinished).add(id) })),

        clearUnseenFinished: (id) =>
          set((s) => {
            if (!s.unseenFinished.has(id)) return s;
            const next = new Set(s.unseenFinished);
            next.delete(id);
            return { unseenFinished: next };
          }),

        newSession: () => {
          const state = get();
          state.abort?.abort();
          if (state.invisible) {
            // Starting a new chat is an exit path (#772): the invisible one is deleted.
            deleteInvisible(state.sessionId);
            sessionStorage.removeItem(INVISIBLE_LIVE_MARKER);
            state.clearUnseenFinished(state.sessionId);
          }
          set({
            sessionId: freshId(),
            invisible: false,
            awaiting: null,
            awaitingDraft: null,
            awaitingApproval: null,
            // A different conversation: whatever the last one was writing isn't this one's.
            liveDocument: null,
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

        toggleInvisible: () => {
          const state = get();
          if (state.invisible) {
            // Toggling off = leaving: delete the invisible chat, start a fresh normal one.
            // newSession owns exactly that exit, so reuse it.
            get().newSession();
            return;
          }
          state.abort?.abort();
          const sessionId = freshId();
          set({
            sessionId,
            invisible: true,
            awaiting: null,
            awaitingDraft: null,
            awaitingApproval: null,
            liveDocument: null,
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
          sessionStorage.setItem(INVISIBLE_LIVE_MARKER, sessionId);
          // Flag it server-side (persist-flagged, so a mid-chat reload keeps the thread).
          // Best-effort: if the mark never lands, the exit's DELETE still erases the chat.
          void api.markEphemeral(sessionId).catch(() => undefined);
        },

        openSession: (id) => {
          const state = get();
          state.abort?.abort();
          if (state.invisible && state.sessionId !== id) {
            // Switching sessions is an exit path (#772): the invisible one is deleted.
            deleteInvisible(state.sessionId);
            sessionStorage.removeItem(INVISIBLE_LIVE_MARKER);
            state.clearUnseenFinished(state.sessionId);
          }
          set((s) => {
            // Opening a session is the one place "the operator has now seen it" is true
            // regardless of how it was opened (sheet row, palette, hover-card) — clear its
            // away-finished marker here rather than at each call site (#492).
            const unseenFinished = s.unseenFinished.has(id)
              ? new Set(s.unseenFinished)
              : s.unseenFinished;
            unseenFinished.delete(id);
            return {
              sessionId: id,
              invisible: false,
              awaiting: null,
              awaitingDraft: null,
              awaitingApproval: null,
              // A different conversation: whatever the last one was writing isn't this one's.
              liveDocument: null,
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
              unseenFinished,
            };
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

        editAndRerun: async (content, model, onDone, messageId) => {
          if (get().streaming) return;
          set({ pendingUser: null, pendingAttachments: [] });
          const sid = encodeURIComponent(get().sessionId);
          await runTurn(
            `/platform/v1/agent/sessions/${sid}/edit`,
            // Omitted, the server falls back to the last user message — #302's behavior.
            { content, model: model ?? undefined, message_id: messageId },
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

        resolveApproval: async (decision, onDone, comment) => {
          const awaitingApproval = get().awaitingApproval;
          if (awaitingApproval === null || get().streaming) return;
          set({ awaitingApproval: null });
          // The operator's own click drives each linked change's decision directly, against the
          // owning module's own review-decision endpoint — the agent never approves its own work
          // (suggestions.py). "review" is the review archetype's one fixed page id (ADR-0033,
          // ADR-0018; every adopting module uses it, e.g. knowledge/notes suggestions.py). A
          // module call failure doesn't block the resume — the operator already decided, so a
          // network hiccup shouldn't strand the turn — but it IS told to the model plainly
          // rather than letting it believe every linked change updated (mirrors the backend's
          // own _send_confirmed_draft failure handling for a declined/failed draft send).
          const failures: string[] = [];
          for (const ref of awaitingApproval.refs) {
            try {
              if (decision === "approved") {
                await api.approveSuggestion(ref.module, "review", ref.ref_id);
              } else {
                await api.rejectSuggestion(ref.module, "review", ref.ref_id);
              }
            } catch (err) {
              const detail = err instanceof Error ? err.message : "the request failed";
              failures.push(`${ref.title}: ${detail}`);
            }
          }
          const trimmed = comment?.trim();
          const note =
            failures.length > 0
              ? `Note: ${failures.join("; ")} — the decision may not have taken effect there; check directly if unsure.`
              : undefined;
          const fullComment = [note, trimmed].filter(Boolean).join(" ");
          await runTurn(
            `/platform/v1/agent/runs/${encodeURIComponent(awaitingApproval.runId)}/approval`,
            fullComment ? { decision, comment: fullComment } : { decision },
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
        // Persisted with the id (#772): a reload mid-invisible-chat must come back invisible,
        // not silently flip the same session into a normal, listed one.
        invisible: state.invisible,
        draft: state.draft,
        awaiting: state.awaiting,
        awaitingDraft: state.awaitingDraft,
        awaitingApproval: state.awaitingApproval,
      }),
    },
  ),
);

/** The sessions-list fetch every `["sessions"]` query uses (#772): passes the live invisible
 *  session (if any) as `active`, so the server's list-read orphan sweep spares it while
 *  erasing crash-stranded ones. In `stores/chat` rather than `lib/api` because it reads this
 *  store's state (api.ts must stay store-free). */
export function fetchSessions(): ReturnType<typeof api.sessions> {
  const state = useChat.getState();
  return api.sessions(state.invisible ? state.sessionId : undefined);
}

/**
 * Mounted once in the chat screen (#772): reconciles a rehydrated invisible chat with how
 * this page came to exist. A same-tab **reload** (the sessionStorage marker survived) resumes
 * it — re-marking server-side, idempotently, so the flag is server truth even if the original
 * mark never landed. A fresh **launch** (marker gone: the tab that owned it closed) treats
 * "closing the app" as the exit it is — the stranded invisible chat is deleted and a fresh
 * normal session starts. Backgrounding a PWA keeps the tab (and marker) alive, so it resumes.
 */
export function useInvisibleLaunchGuard(): void {
  useEffect(() => {
    const state = useChat.getState();
    if (!state.invisible) return;
    if (sessionStorage.getItem(INVISIBLE_LIVE_MARKER) === state.sessionId) {
      void api.markEphemeral(state.sessionId).catch(() => undefined);
    } else {
      state.newSession(); // deletes the stranded invisible chat and starts fresh (see above)
    }
    // Runs once per mount: the guard reconciles rehydrated state, not live changes.
  }, []);
}

// A dot/count prefix on the document title while any answer is unseen, so a backgrounded
// PWA/tab shows it too (#492) — restored the moment nothing is left unseen.
const BASE_TITLE = document.title;

/** Which of `prevActive` dropped out of `nowActive` and aren't `currentId` — sessions that
 *  just finished a turn while unseen since the last poll (#492). A pure diff, exported for
 *  focused testing independent of the poll/effect plumbing around it. */
export function newlyFinished(
  prevActive: Set<string>,
  nowActive: Set<string>,
  currentId: string,
): string[] {
  const out: string[] = [];
  for (const id of prevActive) {
    if (!nowActive.has(id) && id !== currentId) out.push(id);
  }
  return out;
}

/**
 * Mounted once in the shell (#492): polls which sessions are generating
 * (`["active-runs"]`, the same query {@link SessionsSheet} already used gated on being open,
 * now always live) and diffs each result against the previous one via {@link newlyFinished}.
 * Reusing the same query key lets this poll and the sheet's own (while open, on its own
 * shorter interval) share one cache entry rather than compete.
 *
 * No manual visibility plumbing: `refetchInterval` already pauses while the tab is hidden
 * (React Query's default), matching the PowerOrb's own poll (see `stores/connection.ts`).
 */
export function useAwayFinishedWatch(): void {
  const sessionId = useChat((s) => s.sessionId);
  const unseenFinished = useChat((s) => s.unseenFinished);
  const markUnseenFinished = useChat((s) => s.markUnseenFinished);
  const activeRuns = useQuery({
    queryKey: ["active-runs"],
    queryFn: api.activeRuns,
    refetchInterval: 15_000,
  });
  // The previous poll's active set — undefined until the second poll, since a *transition*
  // out of the active set (not just "currently inactive") is what "finished" means.
  const prevActiveRef = useRef<Set<string> | undefined>(undefined);

  useEffect(() => {
    if (!activeRuns.data) return;
    const nowActive = new Set(activeRuns.data.session_ids);
    const prevActive = prevActiveRef.current;
    if (prevActive) {
      for (const id of newlyFinished(prevActive, nowActive, sessionId)) markUnseenFinished(id);
    }
    prevActiveRef.current = nowActive;
  }, [activeRuns.data, sessionId, markUnseenFinished]);

  useEffect(() => {
    document.title = unseenFinished.size > 0 ? `• ${BASE_TITLE}` : BASE_TITLE;
  }, [unseenFinished]);
}
