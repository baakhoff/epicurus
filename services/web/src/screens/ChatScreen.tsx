/**
 * Chat — the main surface. Streams agent turns over SSE: a warming readiness bar and a
 * step-by-step process timeline lead the turn (tokens then settle in behind a pulsing
 * caret), every session is grounded in cross-chat memory via session_id, and the model
 * can be switched mid-conversation.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronDown,
  CloudMoon,
  History,
  Pencil,
  RefreshCw,
  Sparkles,
  SquarePen,
  Square,
  SendHorizonal,
  Trash2,
  Wrench,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { AttachButton, AttachmentPill } from "@/components/AttachMenu";
import {
  EntityRefsContext,
  SourcesPill,
  inlinedRefIds,
  refsById,
} from "@/components/EntityRef";
import { Markdown } from "@/components/Markdown";
import { SuggestionReviewModal } from "@/components/SuggestionReviewModal";
import { ProcessTimeline, ReadinessBar, ThinkingIndicator } from "@/components/TurnActivity";
import {
  Badge,
  Button,
  Card,
  Dot,
  EmptyState,
  Sheet,
  Spinner,
  TextArea,
  cn,
} from "@/components/ui";
import { activityTimeline } from "@/lib/activity";
import { ApiError, api } from "@/lib/api";
import type { Attachment, EntityRef, MessageRecord, PendingSuggestion } from "@/lib/contracts";
import { relativeTime, PROVIDER_MODEL_HINTS, formatBytes } from "@/lib/format";
import { SUGGESTION_VERB, suggestionTarget } from "@/lib/suggestions";
import { useChat, type ActivityItem } from "@/stores/chat";
import { useDownloads } from "@/stores/downloads";
import { usePrefs } from "@/stores/prefs";

const QUOTE =
  "It is not what we have but what we enjoy that constitutes our abundance.";

/* ── assistant turn scaffolding ─────────────────────────────────────────── */

function AssistantRow({ children }: { children: ReactNode }) {
  return (
    <div className="flex gap-3">
      <div className="mt-1.5 font-serif text-[15px] leading-none text-accent select-none">ε</div>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

/**
 * A finished or streaming assistant message: the activity timeline (#121, ADR-0041) over the
 * prose. `runs`/`thinking` are the turn's *process* — fed live from the stream, or from the
 * message's persisted activity when a past conversation is reopened — so the timeline folds
 * to its summary header (rather than vanishing) once the answer is in.
 */
function AssistantBlock({
  text,
  timeline = [],
  streaming,
  entityRefs = [],
}: {
  text: string;
  /** The turn's process (thinking + tool steps) in chronological order (#300). */
  timeline?: ActivityItem[];
  streaming: boolean;
  entityRefs?: EntityRef[];
}) {
  const refsMap = useMemo(() => refsById(entityRefs), [entityRefs]);
  // Refs not already linked inline get a chip row beneath the message, so every
  // referenced entity surfaces exactly once (ADR-0019).
  const rowRefs = useMemo(() => {
    const inlined = inlinedRefIds(text);
    return entityRefs.filter((ref) => !inlined.has(ref.ref_id));
  }, [entityRefs, text]);

  return (
    <AssistantRow>
      {/* The activity timeline folds to its summary header once the answer starts. */}
      {timeline.length > 0 && <ProcessTimeline items={timeline} collapsed={text.length > 0} />}
      <EntityRefsContext.Provider value={refsMap}>
        {text && <Markdown>{text}</Markdown>}
      </EntityRefsContext.Provider>
      {streaming && text.length > 0 && (
        <span className="ep-caret ml-0.5 inline-block h-4 w-2 translate-y-0.5 rounded-[2px] bg-accent" />
      )}
      {rowRefs.length > 0 && <SourcesPill refs={rowRefs} />}
    </AssistantRow>
  );
}

/* ── live (streaming) assistant turn ────────────────────────────────────── */

function LiveTurn() {
  const segments = useChat((s) => s.segments);
  const streaming = useChat((s) => s.streaming);
  const readiness = useChat((s) => s.readiness);
  if (segments.length === 0 && !streaming) return null;

  // Before any thinking, token, or tool: warming progress (#122), then a thinking cue (#121).
  if (streaming && segments.length === 0) {
    return (
      <div className="ep-settle">
        <AssistantRow>
          {readiness && !readiness.ready ? (
            <ReadinessBar readiness={readiness} />
          ) : (
            <ThinkingIndicator />
          )}
        </AssistantRow>
      </div>
    );
  }

  const text = segments.flatMap((s) => (s.kind === "text" ? [s.text] : [])).join("\n");
  // The process timeline = the thinking + tool segments, in the order they streamed (#300);
  // the text segments are the answer, rendered below by AssistantBlock.
  const timeline: ActivityItem[] = segments.flatMap((s): ActivityItem[] =>
    s.kind === "thinking"
      ? [{ kind: "thinking", text: s.text }]
      : s.kind === "tool"
        ? [{ kind: "tool", run: s.run }]
        : [],
  );
  return (
    <div className="ep-settle">
      <AssistantBlock text={text} timeline={timeline} streaming={streaming} />
    </div>
  );
}

/* ── sessions sheet ─────────────────────────────────────────────────────── */

function SessionsSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const openSession = useChat((s) => s.openSession);
  const current = useChat((s) => s.sessionId);
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: api.sessions, enabled: open });
  const remove = useMutation({
    mutationFn: api.deleteSession,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sessions"] }),
  });

  return (
    <Sheet open={open} onClose={onClose} title="Conversations" side="left">
      {sessions.isLoading && <Spinner />}
      {sessions.data?.length === 0 && (
        <p className="text-sm text-ink-dim">Nothing yet — your conversations will gather here.</p>
      )}
      <div className="flex flex-col gap-1">
        {sessions.data?.map((session) => (
          <div
            key={session.id}
            className={cn(
              "group flex items-center gap-2 rounded-(--radius-field) px-2 py-2 hover:bg-surface-2",
              session.id === current && "bg-accent-dim",
            )}
          >
            <button
              className="min-w-0 flex-1 text-left"
              onClick={() => {
                openSession(session.id);
                onClose();
              }}
            >
              <p className="truncate font-serif text-sm text-ink">
                {session.title || "untitled"}
              </p>
              <p className="text-xs text-ink-faint">
                {relativeTime(session.last_at)} · {session.message_count} messages
              </p>
            </button>
            <button
              aria-label={`Delete ${session.title || "conversation"}`}
              onClick={() => remove.mutate(session.id)}
              className="rounded p-1.5 text-ink-faint opacity-0 transition-opacity hover:text-danger group-hover:opacity-100"
            >
              <Trash2 size={15} />
            </button>
          </div>
        ))}
      </div>
    </Sheet>
  );
}

/* ── model picker ───────────────────────────────────────────────────────── */

function ModelPicker() {
  const [open, setOpen] = useState(false);
  const model = usePrefs((s) => s.model);
  const setModel = usePrefs((s) => s.setModel);
  const recents = usePrefs((s) => s.recentModels);
  const [custom, setCustom] = useState("");
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.models(), enabled: open });
  const providers = useQuery({ queryKey: ["providers"], queryFn: api.providers, enabled: open });
  const llmPrefs = useQuery({ queryKey: ["llmPrefs"], queryFn: api.llmPrefs, enabled: open });

  const hosted = providers.data?.filter((p) => !p.local && p.configured) ?? [];
  const visibleModels = models.data?.filter((m) => !m.hidden) ?? [];
  const globalDefault = llmPrefs.data?.global_default;
  const defaultLabel = globalDefault ? `core default (${globalDefault})` : "core default";

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="flex max-w-44 items-center gap-1 rounded-full border border-edge px-2.5 py-1 text-xs text-ink-dim transition-colors hover:border-accent hover:text-accent-strong"
      >
        <span className="truncate">{model ?? "default model"}</span>
        <ChevronDown size={12} className="shrink-0" />
      </button>
      <Sheet open={open} onClose={() => setOpen(false)} title="Model for this chat">
        <div className="flex flex-col gap-4">
          <div>
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-faint">Local</p>
            <div className="flex flex-col gap-1">
              <PickRow label={defaultLabel} active={model === null} onPick={() => { setModel(null); setOpen(false); }} />
              {visibleModels.map((m) => (
                <PickRow
                  key={m.name}
                  label={m.name}
                  loaded={m.loaded}
                  size={m.size}
                  active={model === m.name}
                  onPick={() => {
                    setModel(m.name);
                    setOpen(false);
                  }}
                />
              ))}
              {models.isError && (
                <p className="text-xs text-warn">local runtime unreachable</p>
              )}
            </div>
          </div>

          <div>
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-faint">
              Hosted
            </p>
            {hosted.length === 0 && (
              <p className="text-xs text-ink-dim">
                No provider keys yet — add one under{" "}
                <Link to="/models" className="text-accent-strong underline" onClick={() => setOpen(false)}>
                  Models
                </Link>
                .
              </p>
            )}
            {recents.length > 0 && (
              <div className="mb-2 flex flex-wrap gap-1.5">
                {recents.map((recent) => (
                  <button
                    key={recent}
                    onClick={() => {
                      setModel(recent);
                      setOpen(false);
                    }}
                    className="rounded-full border border-edge px-2.5 py-1 text-xs text-ink-dim hover:border-accent hover:text-accent-strong"
                  >
                    {recent}
                  </button>
                ))}
              </div>
            )}
            {hosted.length > 0 && (
              <form
                className="flex gap-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (!custom.trim()) return;
                  setModel(custom.trim());
                  setCustom("");
                  setOpen(false);
                }}
              >
                <input
                  value={custom}
                  onChange={(e) => setCustom(e.target.value)}
                  placeholder={PROVIDER_MODEL_HINTS[hosted[0]?.alias] ?? "provider/model-id"}
                  className="w-full rounded-(--radius-field) border border-edge bg-surface-2 px-3 py-2 text-sm text-ink placeholder:text-ink-faint focus:border-accent focus:outline-none"
                />
                <Button type="submit" variant="outline">
                  Use
                </Button>
              </form>
            )}
          </div>
        </div>
      </Sheet>
    </>
  );
}

function PickRow({
  label,
  active,
  loaded = false,
  size = null,
  onPick,
}: {
  label: string;
  active: boolean;
  loaded?: boolean;
  size?: number | null;
  onPick: () => void;
}) {
  return (
    <button
      onClick={onPick}
      className={cn(
        "flex items-center justify-between rounded-(--radius-field) px-3 py-2 text-left text-sm",
        active ? "bg-accent-dim text-accent-strong" : "text-ink hover:bg-surface-2",
      )}
    >
      <span className="truncate">{label}</span>
      <span className="flex shrink-0 items-center gap-2">
        {size != null && <span className="text-xs text-ink-faint">{formatBytes(size)}</span>}
        {loaded && <Badge tone="ok">loaded</Badge>}
        {active && <Check size={14} />}
      </span>
    </button>
  );
}

/* ── first-run welcome ──────────────────────────────────────────────────── */

function Welcome() {
  const pull = useDownloads((s) => s.pull);
  const active = useDownloads((s) => s.active);
  const queryClient = useQueryClient();
  const suggestions = ["llama3.2", "qwen2.5:0.5b"];

  return (
    <EmptyState quote={QUOTE}>
      <Card className="mt-2 w-full max-w-sm text-left">
        <h3 className="font-serif text-base text-ink">Welcome to the garden</h3>
        <p className="mt-1 text-sm leading-relaxed text-ink-dim">
          No model lives here yet. Pull a local one — private, yours — or add a
          hosted provider key under <Link to="/models" className="text-accent-strong underline">Models</Link>.
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          {suggestions.map((name) => {
            const download = active[name];
            const pct =
              download?.total && download.completed != null
                ? Math.round((download.completed / download.total) * 100)
                : null;
            return (
              <Button
                key={name}
                variant="outline"
                busy={Boolean(download && !download.done)}
                onClick={() =>
                  pull(name, () => queryClient.invalidateQueries({ queryKey: ["models"] }))
                }
              >
                {download && !download.done
                  ? pct != null
                    ? `${name} — ${pct}%`
                    : `${name}…`
                  : `Pull ${name}`}
              </Button>
            );
          })}
        </div>
      </Card>
    </EmptyState>
  );
}

/* ── suggestion bubble (#KB-refactor) ───────────────────────────────────────── */

/**
 * A bubble above the composer when the assistant has filed suggestions (ADR-0033). A
 * one-tap structural op (move / new folder / new knowledge base) can be approved inline;
 * a richer change opens the review overlay. Reject discards the suggestion outright without
 * opening anything (#341); Ignore just hides the bubble, leaving it on the Suggestions page.
 */
export function SuggestionBubble() {
  const qc = useQueryClient();
  const pending = useQuery({
    queryKey: ["suggestions"],
    queryFn: api.suggestions,
    staleTime: 30_000,
  });
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [reviewing, setReviewing] = useState<PendingSuggestion | null>(null);
  const invalidate = () => void qc.invalidateQueries({ queryKey: ["suggestions"] });

  const approveSimple = useMutation({
    mutationFn: (s: PendingSuggestion) => api.approveSuggestion(s.module, s.page_id, s.id),
    onSuccess: invalidate,
    onError: (e) => window.alert(e instanceof ApiError ? e.detail : "Could not approve."),
  });

  // Reject discards the suggestion server-side and never opens the review overlay (#341) —
  // for any proposal type, including folder / knowledge-base creation.
  const reject = useMutation({
    mutationFn: (s: PendingSuggestion) => api.rejectSuggestion(s.module, s.page_id, s.id),
    onSuccess: invalidate,
    onError: (e) => window.alert(e instanceof ApiError ? e.detail : "Could not reject."),
  });
  const busy = approveSimple.isPending || reject.isPending;

  const active = (pending.data ?? []).filter((s) => !dismissed.has(s.id));
  const latest = active.at(-1);
  if (!latest) return null;

  const simple =
    latest.operation === "move" ||
    latest.operation === "mkdir" ||
    latest.operation === "mkproject";
  const target = suggestionTarget(latest);

  return (
    <>
      <div className="mx-auto mb-2 flex max-w-2xl items-center gap-2 rounded-(--radius-card) border border-accent/40 bg-accent-dim px-3 py-2 text-sm">
        <Sparkles size={15} className="shrink-0 text-accent" />
        <span className="min-w-0 flex-1 truncate text-ink">
          {active.length > 1 && (
            <span className="text-ink-faint">{active.length} suggestions · </span>
          )}
          The assistant wants to {SUGGESTION_VERB[latest.operation]}{" "}
          <span className="font-mono text-xs">{target}</span>
        </span>
        {simple ? (
          <Button
            variant="primary"
            className="h-7 shrink-0 px-2.5 py-0 text-xs"
            disabled={busy}
            busy={approveSimple.isPending}
            onClick={() => approveSimple.mutate(latest)}
          >
            Approve
          </Button>
        ) : (
          <Button
            variant="primary"
            className="h-7 shrink-0 px-2.5 py-0 text-xs"
            disabled={busy}
            onClick={() => setReviewing(latest)}
          >
            Open
          </Button>
        )}
        <Button
          variant="outline"
          className="h-7 shrink-0 px-2.5 py-0 text-xs"
          disabled={busy}
          busy={reject.isPending}
          onClick={() => reject.mutate(latest)}
        >
          Reject
        </Button>
        <Button
          variant="ghost"
          className="h-7 shrink-0 px-2.5 py-0 text-xs"
          disabled={busy}
          onClick={() => setDismissed((p) => new Set(p).add(latest.id))}
        >
          Ignore
        </Button>
      </div>
      {reviewing && (
        <SuggestionReviewModal
          key={reviewing.id}
          suggestion={reviewing}
          onClose={() => setReviewing(null)}
          onResolved={invalidate}
        />
      )}
    </>
  );
}

/* ── the screen ─────────────────────────────────────────────────────────── */

export function ChatScreen() {
  const queryClient = useQueryClient();
  const chat = useChat();
  const model = usePrefs((s) => s.model);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [sessionsOpen, setSessionsOpen] = useState(false);
  // Inline edit of the last user message (#302): the index being edited + its draft text.
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [editText, setEditText] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const pinnedRef = useRef(true);

  // The composer text lives in the chat store so it survives leaving and returning
  // to the page. The textarea's auto-grown height is set imperatively on keystroke,
  // so restore it on mount when we come back to a saved (possibly multi-line) draft.
  useEffect(() => {
    const el = composerRef.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, 144)}px`;
    }
  }, []);

  const history = useQuery({
    queryKey: ["session", chat.sessionId],
    queryFn: () => api.sessionMessages(chat.sessionId),
  });
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.models() });
  const providers = useQuery({ queryKey: ["providers"], queryFn: api.providers });
  const llmPrefs = useQuery({ queryKey: ["llmPrefs"], queryFn: api.llmPrefs });

  // The model this chat will actually use (the per-chat choice, else the core default). If it's
  // a local one, check whether it can call tools so we can warn that it's chat-only.
  const effectiveModel = model ?? llmPrefs.data?.global_default ?? null;
  const effectiveIsLocal = Boolean(effectiveModel) && !effectiveModel!.includes("/");
  const modelDetails = useQuery({
    queryKey: ["modelDetails", effectiveModel],
    queryFn: () => api.modelDetails(effectiveModel!),
    enabled: effectiveIsLocal,
  });
  const caps = modelDetails.data?.capabilities ?? [];
  // Only warn when the runtime actually reported capabilities and tools isn't among them —
  // an empty list means "unknown", not "no tools".
  const toolless = effectiveIsLocal && caps.length > 0 && !caps.includes("tools");

  const hasAnyBrain =
    (models.data?.length ?? 0) > 0 ||
    (providers.data?.some((p) => !p.local && p.configured) ?? false);
  const firstRun =
    models.isSuccess && providers.isSuccess && !hasAnyBrain && (history.data?.length ?? 0) === 0;

  // Keep the view pinned to the bottom while streaming — unless the reader
  // scrolled up to re-read; never hijack their position.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);
  useEffect(() => {
    const el = scrollRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [chat.segments, chat.pendingUser, history.data]);

  const send = () => {
    const text = chat.draft.trim();
    if (!text || chat.streaming) return;
    const sent = attachments;
    setAttachments([]); // chat.send clears the draft itself
    pinnedRef.current = true;
    // chat.send clears the draft, but the textarea's height was grown imperatively on
    // each keystroke — clear the inline height so an emptied composer snaps back to one
    // line (min-h-[42px]) instead of keeping its multi-line height. Same on mobile + desktop.
    const composer = composerRef.current;
    if (composer) composer.style.height = "";
    void chat.send(
      text,
      model,
      async () => {
        await queryClient.refetchQueries({ queryKey: ["session", chat.sessionId] });
        void queryClient.invalidateQueries({ queryKey: ["sessions"] });
        // A turn may have filed knowledge-base suggestions — refresh the composer bubble.
        void queryClient.invalidateQueries({ queryKey: ["suggestions"] });
      },
      sent,
    );
  };

  // While a turn streams, history already contains the just-sent user message —
  // suppress the optimistic copy once the server history catches up.
  const messages = history.data ?? [];
  const showPending =
    chat.pendingUser !== null &&
    (chat.streaming || messages[messages.length - 1]?.content !== chat.pendingUser);

  // Regenerate attaches to the last assistant message; Edit to the last user message.
  const lastAssistantIdx = messages.reduce((a, m, i) => (m.role === "assistant" ? i : a), -1);
  const lastUserIdx = messages.reduce((a, m, i) => (m.role === "user" ? i : a), -1);
  const turnControlsVisible = !chat.streaming && !showPending && editingIdx === null;

  const onTurnDone = async () => {
    await queryClient.refetchQueries({ queryKey: ["session", chat.sessionId] });
    void queryClient.invalidateQueries({ queryKey: ["sessions"] });
  };

  // Regenerate: optimistically drop the stale answer (everything after the last user turn),
  // then stream a fresh one. The server truncates the same tail before re-answering (#302).
  const regenerate = () => {
    if (chat.streaming || lastUserIdx < 0) return;
    queryClient.setQueryData<MessageRecord[]>(["session", chat.sessionId], (old) =>
      (old ?? []).slice(0, lastUserIdx + 1),
    );
    pinnedRef.current = true;
    void chat.regenerate(model, onTurnDone);
  };

  const cancelEdit = () => {
    setEditingIdx(null);
    setEditText("");
  };

  // Save an edited last user message: optimistically show the corrected text (and drop the
  // old answer), then stream the new answer. The server edits + truncates server-side.
  const saveEdit = () => {
    const content = editText.trim();
    if (!content || chat.streaming || lastUserIdx < 0) return;
    setEditingIdx(null);
    queryClient.setQueryData<MessageRecord[]>(["session", chat.sessionId], (old) => {
      const trimmed = (old ?? []).slice(0, lastUserIdx + 1);
      const last = trimmed[trimmed.length - 1];
      if (last) trimmed[trimmed.length - 1] = { ...last, content };
      return trimmed;
    });
    pinnedRef.current = true;
    void chat.editAndRerun(content, model, onTurnDone);
  };

  return (
    <div className="flex h-full flex-col">
      {/* chat header row */}
      <div className="flex items-center justify-between gap-2 border-b border-edge px-4 py-2">
        <div className="flex items-center gap-2">
          <button
            onClick={() => setSessionsOpen(true)}
            aria-label="Conversations"
            className="rounded-md p-1.5 text-ink-dim hover:bg-surface-2 hover:text-ink"
          >
            <History size={18} />
          </button>
          <button
            onClick={() => chat.newSession()}
            aria-label="New chat"
            className="rounded-md p-1.5 text-ink-dim hover:bg-surface-2 hover:text-ink"
          >
            <SquarePen size={18} />
          </button>
        </div>
        <ModelPicker />
      </div>

      {/* transcript */}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        <div className="mx-auto flex max-w-2xl flex-col gap-5">
          {firstRun && <Welcome />}
          {!firstRun && messages.length === 0 && !chat.pendingUser && (
            <EmptyState quote={QUOTE}>
              <p className="text-xs text-ink-faint">a new conversation — it will remember</p>
            </EmptyState>
          )}
          {messages.map((message, i) =>
            message.role === "user" ? (
              editingIdx === i ? (
                <div key={i} className="flex w-full flex-col items-end gap-2">
                  <TextArea
                    value={editText}
                    autoFocus
                    aria-label="Edit message"
                    className="w-full max-w-[85%] min-h-[60px] text-[15px]"
                    onChange={(e) => setEditText(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        saveEdit();
                      } else if (e.key === "Escape") {
                        cancelEdit();
                      }
                    }}
                  />
                  <div className="flex gap-2">
                    <Button variant="ghost" onClick={cancelEdit}>
                      Cancel
                    </Button>
                    <Button variant="primary" onClick={saveEdit} disabled={!editText.trim()}>
                      Resend
                    </Button>
                  </div>
                </div>
              ) : (
                <div key={i} className="flex flex-col items-end gap-1">
                  <div className="max-w-[85%] rounded-2xl rounded-br-md bg-user-bubble px-4 py-2.5 text-[15px] leading-relaxed whitespace-pre-wrap">
                    {message.content}
                  </div>
                  {message.attachments.length > 0 && (
                    <div className="flex max-w-[85%] flex-wrap justify-end gap-1.5">
                      {message.attachments.map((a) => (
                        <AttachmentPill key={a.att_id} attachment={a} />
                      ))}
                    </div>
                  )}
                  {i === lastUserIdx && turnControlsVisible && (
                    <button
                      aria-label="Edit message"
                      onClick={() => {
                        setEditingIdx(i);
                        setEditText(message.content);
                      }}
                      className="flex items-center gap-1 text-[11px] text-ink-faint hover:text-ink"
                    >
                      <Pencil size={12} /> Edit
                    </button>
                  )}
                </div>
              )
            ) : (
              <div key={i}>
                <AssistantBlock
                  text={message.content}
                  timeline={activityTimeline(message.activity)}
                  streaming={false}
                  entityRefs={message.entity_refs}
                />
                {i === lastAssistantIdx && turnControlsVisible && (
                  <button
                    aria-label="Regenerate response"
                    onClick={regenerate}
                    className="mt-1 ml-7 flex items-center gap-1 text-[11px] text-ink-faint hover:text-ink"
                  >
                    <RefreshCw size={12} /> Regenerate
                  </button>
                )}
              </div>
            ),
          )}
          {showPending && (
            <div className="flex flex-col items-end gap-1">
              <div className="max-w-[85%] rounded-2xl rounded-br-md bg-user-bubble px-4 py-2.5 text-[15px] leading-relaxed whitespace-pre-wrap">
                {chat.pendingUser}
              </div>
              {chat.pendingAttachments.length > 0 && (
                <div className="flex max-w-[85%] flex-wrap justify-end gap-1.5">
                  {chat.pendingAttachments.map((a) => (
                    <AttachmentPill key={a.att_id} attachment={a} />
                  ))}
                </div>
              )}
            </div>
          )}
          <LiveTurn />
          {chat.error && (
            <Card className={cn("text-sm", chat.paused ? "border-accent/40" : "border-danger/40")}>
              {chat.paused ? (
                <div className="flex items-start gap-3">
                  <CloudMoon size={18} className="mt-0.5 shrink-0 text-accent" />
                  <div>
                    <p className="text-ink">epicurus is asleep.</p>
                    <p className="mt-0.5 text-ink-dim">
                      Wake it from the power toggle, or pick a hosted model that can answer
                      while the garden rests.
                    </p>
                  </div>
                </div>
              ) : (
                <p className="text-danger">{chat.error}</p>
              )}
            </Card>
          )}
          <div className="h-2" />
        </div>
      </div>

      {/* composer */}
      <div className="border-t border-edge px-4 py-3 pb-safe">
        {toolless && (
          <div className="mx-auto mb-2 flex max-w-2xl items-center gap-1.5 rounded-full border border-edge bg-surface-2 px-3 py-1 text-[11px] text-ink-dim">
            <Wrench size={12} className="shrink-0 text-ink-faint" />
            <span>
              <span className="font-medium text-ink">{effectiveModel}</span> can't use tools — it
              can only chat (no calendar, files, or other actions).
            </span>
          </div>
        )}
        <SuggestionBubble />
        {attachments.length > 0 && (
          <div className="mx-auto mb-2 flex max-w-2xl flex-wrap gap-1.5">
            {attachments.map((a) => (
              <AttachmentPill
                key={a.att_id}
                attachment={a}
                onRemove={() =>
                  setAttachments((prev) => prev.filter((x) => x.att_id !== a.att_id))
                }
              />
            ))}
          </div>
        )}
        <div className="mx-auto flex max-w-2xl items-end gap-2">
          <AttachButton onAttach={(a) => setAttachments((prev) => [...prev, a])} />
          <TextArea
            ref={composerRef}
            rows={1}
            value={chat.draft}
            onChange={(e) => {
              chat.setDraft(e.target.value);
              e.target.style.height = "auto";
              e.target.style.height = `${Math.min(e.target.scrollHeight, 144)}px`;
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            placeholder={chat.paused ? "asleep — wake to chat locally" : "Ask anything…"}
            aria-label="Message"
            className="max-h-36 min-h-[42px] text-[16px]"
          />
          {chat.streaming ? (
            <Button variant="outline" aria-label="Stop" onClick={chat.stop} className="h-[42px]">
              <Square size={15} />
            </Button>
          ) : (
            <Button
              variant="primary"
              aria-label="Send"
              onClick={send}
              disabled={!chat.draft.trim()}
              className="h-[42px]"
            >
              <SendHorizonal size={16} />
            </Button>
          )}
        </div>
        <p className="mx-auto mt-1.5 max-w-2xl text-center text-[10px] text-ink-faint sm:text-left">
          <Dot tone={chat.streaming ? "accent" : "dim"} /> memory on — this conversation is
          remembered across chats
        </p>
      </div>

      <SessionsSheet open={sessionsOpen} onClose={() => setSessionsOpen(false)} />
    </div>
  );
}
