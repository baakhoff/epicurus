/**
 * Chat — the main surface. Streams agent turns over SSE: a warming readiness bar and a
 * step-by-step process timeline lead the turn (tokens then settle in behind a pulsing
 * caret), every session is grounded in cross-chat memory via session_id, and the model
 * can be switched mid-conversation.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDown,
  Check,
  ChevronDown,
  CircleHelp,
  CloudMoon,
  Copy,
  Eye,
  History,
  Paperclip,
  Pencil,
  RefreshCw,
  Sparkles,
  SquarePen,
  Square,
  SendHorizonal,
  Trash2,
  WifiOff,
  Wrench,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type ReactNode,
} from "react";
import { Link, useSearchParams } from "react-router-dom";

import { AttachButton, AttachmentPill, PendingAttachmentPill } from "@/components/AttachMenu";
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
  Confirm,
  Dot,
  EmptyState,
  Sheet,
  Spinner,
  TextArea,
  TextInput,
  Tooltip,
  cn,
} from "@/components/ui";
import { activityTimeline } from "@/lib/activity";
import { ApiError, api } from "@/lib/api";
import { copyText } from "@/lib/clipboard";
import type {
  Attachment,
  EntityRef,
  MessageRecord,
  ModuleSnapshot,
  PendingSuggestion,
  SessionSummary,
} from "@/lib/contracts";
import {
  RECENCY_BUCKETS,
  recencyBucket,
  relativeTime,
  isHostedModelId,
  PROVIDER_MODEL_HINTS,
  formatBytes,
} from "@/lib/format";
import { SHARE_CACHE, SHARE_FILE_KEY, SHARE_FILE_NAME_HEADER, SHARE_META_KEY, type ShareMeta } from "@/lib/shareTarget";
import { SUGGESTION_VERB, suggestionTarget } from "@/lib/suggestions";
import { useChat, type ActivityItem } from "@/stores/chat";
import { useConnection } from "@/stores/connection";
import { useDownloads } from "@/stores/downloads";
import { usePanel } from "@/stores/panel";
import { usePrefs } from "@/stores/prefs";
import { toast } from "@/stores/toasts";

// A small rotation keeps the garden fresh without ever surprising twice in one day:
// the quote is picked by day-of-year, so it changes overnight, never mid-session.
const QUOTES = [
  "It is not what we have but what we enjoy that constitutes our abundance.",
  "Do not spoil what you have by desiring what you have not.",
  "Of all the means to insure happiness throughout the whole life, by far the most important is the acquisition of friends.",
  "Nothing is enough for the man to whom enough is too little.",
];

function dayQuote(now: Date = new Date()): string {
  const start = new Date(now.getFullYear(), 0, 0);
  const dayOfYear = Math.floor((now.getTime() - start.getTime()) / 86_400_000);
  return QUOTES[dayOfYear % QUOTES.length];
}

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

/* ── ask_user clarifying prompt (ADR-0053, #360) ────────────────────────── */

/**
 * The inline prompt shown when a turn pauses on `ask_user` (ADR-0053): the assistant's
 * question and an input to answer it, rendered as part of the live turn beneath the partial
 * answer. Submitting resumes the suspended run (`chat.resume`) and the turn continues
 * streaming; the main composer stays available as an escape hatch (it abandons the question).
 */
function AskUserPrompt({
  question,
  onSubmit,
}: {
  question: string;
  onSubmit: (answer: string) => void;
}) {
  const [answer, setAnswer] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // Same connection gate as the main composer (#494): resuming a suspended turn is just
  // as send-adjacent as the composer's own Send, so it fails the same way if ungated (#530).
  const connectionLost = useConnection((s) => s.coreDown || !s.online);
  useEffect(() => {
    inputRef.current?.focus();
  }, []);
  const submit = () => {
    const text = answer.trim();
    if (!text || connectionLost) return;
    setAnswer("");
    onSubmit(text);
  };
  return (
    <AssistantRow>
      {/* A plain div, not `Card`: `cn` doesn't tailwind-merge, so a Card's base `border-edge`
          would win over an accent override — set the accent border directly instead. */}
      <div className="rounded-(--radius-card) border border-accent/40 bg-surface p-4">
        <div className="flex items-start gap-2.5">
          <CircleHelp size={16} className="mt-0.5 shrink-0 text-accent" />
          <div className="min-w-0 flex-1">
            <p className="text-sm leading-relaxed text-ink">
              {question || "The assistant needs a little more to go on."}
            </p>
            <div className="mt-2.5 flex items-end gap-2">
              <TextArea
                ref={inputRef}
                rows={1}
                value={answer}
                onChange={(e) => {
                  setAnswer(e.target.value);
                  e.target.style.height = "auto";
                  e.target.style.height = `${Math.min(e.target.scrollHeight, 144)}px`;
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    submit();
                  }
                }}
                placeholder="Your answer…"
                aria-label="Answer the assistant's question"
                className="max-h-36 min-h-[42px] text-[15px]"
              />
              <Button
                variant="primary"
                aria-label="Send answer"
                onClick={submit}
                disabled={!answer.trim() || connectionLost}
                className="h-[42px]"
              >
                <SendHorizonal size={16} />
              </Button>
            </div>
          </div>
        </div>
      </div>
    </AssistantRow>
  );
}

/* ── sessions sheet ─────────────────────────────────────────────────────── */

function SessionRow({
  session,
  current,
  running,
  unseen,
  onOpen,
  onDelete,
}: {
  session: SessionSummary;
  current: boolean;
  running: boolean;
  /** Finished a turn while this wasn't the open session (#492) — cleared once opened. */
  unseen: boolean;
  onOpen: () => void;
  onDelete: () => void;
}) {
  return (
    <div
      className={cn(
        "group/session flex items-center gap-2 rounded-(--radius-field) px-2 py-2 hover:bg-surface-2",
        current && "bg-accent-dim",
      )}
    >
      <button className="min-w-0 flex-1 text-left" onClick={onOpen}>
        <p className="flex items-center gap-1.5 font-serif text-sm text-ink">
          {running ? (
            <span
              title="Generating…"
              aria-label="Generating"
              className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-accent"
            />
          ) : (
            unseen && (
              <span title="Finished — unseen" aria-label="Finished, unseen" className="shrink-0">
                <Dot tone="accent" />
              </span>
            )
          )}
          <span className={cn("min-w-0 truncate", unseen && !running && "font-medium")}>
            {session.title || "untitled"}
          </span>
        </p>
        <p className="text-xs text-ink-faint">
          {relativeTime(session.last_at)} · {session.message_count} messages
        </p>
      </button>
      <button
        aria-label={`Delete ${session.title || "conversation"}`}
        onClick={onDelete}
        className="rounded p-1.5 text-ink-faint opacity-0 transition-opacity hover:text-danger group-hover/session:opacity-100 focus-visible:opacity-100"
      >
        <Trash2 size={15} />
      </button>
    </div>
  );
}

function SessionsSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const openSession = useChat((s) => s.openSession);
  const newSession = useChat((s) => s.newSession);
  const current = useChat((s) => s.sessionId);
  const streaming = useChat((s) => s.streaming);
  // Rows finished elsewhere while unseen (#492) — the shell-level watcher keeps this current
  // regardless of whether this sheet has been open; `openSession` below clears each on open.
  const unseenFinished = useChat((s) => s.unseenFinished);
  const [query, setQuery] = useState("");
  // Deleting a whole conversation from a hover-revealed icon is one misclick away from the
  // row's open-target, so it always confirms first (#480).
  const [confirming, setConfirming] = useState<SessionSummary | null>(null);
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: api.sessions, enabled: open });
  // Which conversations are generating right now (#396): poll while the list is open so a turn
  // finishing in another session updates here too. The current session also reflects its own
  // live `streaming` immediately (union below), without waiting for the next poll.
  const activeRuns = useQuery({
    queryKey: ["active-runs"],
    queryFn: api.activeRuns,
    enabled: open,
    refetchInterval: open ? 3000 : false,
  });
  const running = new Set(activeRuns.data?.session_ids ?? []);
  if (streaming) running.add(current);
  const remove = useMutation({
    mutationFn: api.deleteSession,
    onSuccess: (_result, id) => {
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
      // Drop any away-finished marker for the deleted session — openSession is the only other
      // place it clears, and a marked session is never the current one, so without this a
      // delete-while-unseen would strand the History dot + title prefix until a reload (#492).
      useChat.getState().clearUnseenFinished(id);
      // Deleting the open conversation would leave the transcript orphaned on screen —
      // start fresh instead, exactly like the New-chat button.
      if (id === current) newSession();
    },
  });

  const needle = query.trim().toLowerCase();
  const matching = (sessions.data ?? []).filter(
    (s) => !needle || (s.title || "untitled").toLowerCase().includes(needle),
  );
  // Grouped by recency when browsing; a search shows one flat result list instead.
  const groups = RECENCY_BUCKETS.map((bucket) => ({
    bucket,
    items: matching.filter((s) => recencyBucket(s.last_at) === bucket),
  })).filter((g) => g.items.length > 0);

  const row = (session: SessionSummary) => (
    <SessionRow
      key={session.id}
      session={session}
      current={session.id === current}
      running={running.has(session.id)}
      unseen={unseenFinished.has(session.id)}
      onOpen={() => {
        openSession(session.id);
        onClose();
      }}
      onDelete={() => setConfirming(session)}
    />
  );

  return (
    <Sheet open={open} onClose={onClose} title="Conversations" side="left">
      {sessions.isLoading && <Spinner />}
      {sessions.data?.length === 0 && (
        <p className="text-sm text-ink-dim">Nothing yet — your conversations will gather here.</p>
      )}
      {(sessions.data?.length ?? 0) > 0 && (
        <TextInput
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search conversations…"
          aria-label="Search conversations"
          className="mb-3"
        />
      )}
      {needle ? (
        <div className="flex flex-col gap-1">
          {matching.length === 0 && (
            <p className="text-sm text-ink-dim">Nothing matches “{query.trim()}”.</p>
          )}
          {matching.map(row)}
        </div>
      ) : (
        groups.map(({ bucket, items }) => (
          <div key={bucket} className="mb-3">
            <p className="mb-1 px-2 text-xs font-medium uppercase tracking-wide text-ink-faint">
              {bucket}
            </p>
            <div className="flex flex-col gap-1">{items.map(row)}</div>
          </div>
        ))
      )}
      <Confirm
        open={confirming !== null}
        message={`Delete “${confirming?.title || "this conversation"}”? The whole conversation is removed.`}
        confirmLabel="Delete"
        danger
        onConfirm={() => {
          if (confirming) remove.mutate(confirming.id);
          setConfirming(null);
        }}
        onCancel={() => setConfirming(null)}
      />
    </Sheet>
  );
}

/* ── model picker ───────────────────────────────────────────────────────── */

function ModelPicker() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const model = usePrefs((s) => s.model);
  const setModel = usePrefs((s) => s.setModel);
  const recents = usePrefs((s) => s.recentModels);
  const [custom, setCustom] = useState("");
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.models(), enabled: open });
  const providers = useQuery({ queryKey: ["providers"], queryFn: api.providers, enabled: open });
  const llmPrefs = useQuery({ queryKey: ["llmPrefs"], queryFn: api.llmPrefs, enabled: open });
  const saved = useQuery({ queryKey: ["savedModels"], queryFn: api.savedModels, enabled: open });

  const hosted = providers.data?.filter((p) => !p.local && p.configured) ?? [];
  const visibleModels = models.data?.filter((m) => !m.hidden) ?? [];
  const globalDefault = llmPrefs.data?.global_default;
  const defaultLabel = globalDefault ? `core default (${globalDefault})` : "core default";

  // The hosted rows: the server's saved list (source of truth, #496) first, then any
  // device-local recents not yet in it — an instant echo before the query resolves / on an
  // older core with no saved-models endpoint.
  const savedIds = saved.data?.map((s) => s.model) ?? [];
  const hostedIds = [...savedIds, ...recents.filter((r) => !savedIds.includes(r))];

  // Auto-save a hosted id on use, so it's offered on every device next time (#496). Idempotent
  // (a re-use just bumps recency); guarded by isHostedModelId so a bare/local free-text entry is
  // never sent to the hosted-only endpoint (which would 400).
  const save = useMutation({
    mutationFn: api.addSavedModel,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["savedModels"] }),
  });
  const chooseHosted = (id: string) => {
    setModel(id);
    if (isHostedModelId(id)) save.mutate(id);
    setOpen(false);
  };

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
            {hosted.length === 0 && hostedIds.length === 0 && (
              <p className="text-xs text-ink-dim">
                No provider keys yet — add one under{" "}
                <Link to="/models" className="text-accent-strong underline" onClick={() => setOpen(false)}>
                  Models
                </Link>
                .
              </p>
            )}
            {hostedIds.length > 0 && (
              <div className="mb-2 flex flex-col gap-1">
                {hostedIds.map((id) => (
                  <PickRow key={id} label={id} active={model === id} onPick={() => chooseHosted(id)} />
                ))}
              </div>
            )}
            {hosted.length > 0 && (
              <form
                className="flex gap-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (!custom.trim()) return;
                  chooseHosted(custom.trim());
                  setCustom("");
                }}
              >
                <TextInput
                  value={custom}
                  onChange={(e) => setCustom(e.target.value)}
                  placeholder={PROVIDER_MODEL_HINTS[hosted[0]?.alias] ?? "provider/model-id"}
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
    <EmptyState quote={dayQuote()}>
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

/* ── starter prompts (empty conversation, #480) ─────────────────────────── */

/**
 * What a fresh conversation can offer, drawn from the modules that are actually
 * installed (enabled + healthy). The mapping is shell-owned — modules only exist in it
 * by name, consistent with the icon vocabulary (ADR-0018). Prompts ending in a space
 * are deliberate openers: the chip fills the composer and leaves the cursor waiting.
 */
const STARTERS: Array<{ module: string; label: string; prompt: string }> = [
  { module: "calendar", label: "What's on this week?", prompt: "What's on my calendar this week?" },
  { module: "mail", label: "Anything important in mail?", prompt: "Anything important in my mail today?" },
  { module: "tasks", label: "Plan my day", prompt: "Look at my tasks and help me plan today." },
  { module: "knowledge", label: "Ask my knowledge base", prompt: "Search my knowledge base for " },
  { module: "notes", label: "Capture a note", prompt: "Add to my notes: " },
  { module: "websearch", label: "Search the web", prompt: "Search the web for " },
];

function StarterPrompts({
  modules,
  onPick,
}: {
  modules: ModuleSnapshot[];
  onPick: (prompt: string) => void;
}) {
  const available = new Set(
    modules.filter((m) => m.status.healthy && m.enabled).map((m) => m.manifest.name),
  );
  const starters = STARTERS.filter((s) => available.has(s.module)).slice(0, 4);
  if (starters.length === 0) return null;
  return (
    <div className="flex max-w-sm flex-wrap justify-center gap-1.5">
      {starters.map((s) => (
        <button
          key={s.module}
          onClick={() => onPick(s.prompt)}
          className="rounded-full border border-edge px-3 py-1.5 text-xs text-ink-dim transition-colors hover:border-accent hover:text-accent-strong"
        >
          {s.label}
        </button>
      ))}
    </div>
  );
}

/* ── copy an assistant turn (#480) ──────────────────────────────────────── */

function CopyMessage({ text, className }: { text: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void copyText(text).then((ok) => {
      if (!ok) return;
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };
  return (
    <button
      aria-label="Copy message"
      onClick={copy}
      className={cn("flex items-center gap-1 text-[11px] text-ink-faint hover:text-ink", className)}
    >
      {copied ? <Check size={12} className="text-ok" /> : <Copy size={12} />} Copy
    </button>
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
    onError: (e) => toast.error(e instanceof ApiError ? e.detail : "Could not approve."),
  });

  // Reject discards the suggestion server-side and never opens the review overlay (#341) —
  // for any proposal type, including folder / knowledge-base creation.
  const reject = useMutation({
    mutationFn: (s: PendingSuggestion) => api.rejectSuggestion(s.module, s.page_id, s.id),
    onSuccess: invalidate,
    onError: (e) => toast.error(e instanceof ApiError ? e.detail : "Could not reject."),
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
  // Share target deep-link (#493): ?share=1 after the SW's POST-redirect (src/sw.ts).
  const [searchParams, setSearchParams] = useSearchParams();
  // Offline / core-unreachable (#494): the shell banner explains which; here the composer
  // keeps the draft (it already persists) and disables Send instead of letting the message
  // fail into an error card.
  const connectionLost = useConnection((s) => s.coreDown || !s.online);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [sessionsOpen, setSessionsOpen] = useState(false);
  // Paste/drop uploads in flight (#489): rendered as spinner pills until the server
  // answers. The drag depth counter survives dragenter/dragleave pairs firing on every
  // child the pointer crosses — plain boolean state flickers.
  const [uploading, setUploading] = useState<Array<{ id: number; name: string }>>([]);
  const [dragActive, setDragActive] = useState(false);
  const uploadSeq = useRef(0);
  const dragDepth = useRef(0);
  // Inline edit of a user message (#302, #552): the index being edited + its draft text.
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [editText, setEditText] = useState("");
  // A mid-history edit awaiting confirmation (#552) — held here until the user accepts losing
  // the turns after it, since applying it is what discards them.
  const [confirmingEdit, setConfirmingEdit] = useState<{ idx: number; content: string } | null>(
    null,
  );
  const scrollRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const pinnedRef = useRef(true);
  // Mirrors pinnedRef for rendering (the ref stays authoritative in scroll handlers):
  // when the reader scrolls up, a "jump to latest" affordance appears (#480).
  const [pinned, setPinned] = useState(true);
  const pin = () => {
    pinnedRef.current = true;
    setPinned(true);
  };

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
  // The open conversation's title for the header (#480) — same cache key the sessions
  // sheet uses, and the send/turn-done invalidations keep it fresh once a title lands.
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: api.sessions, staleTime: 15_000 });
  const sessionTitle = sessions.data?.find((s) => s.id === chat.sessionId)?.title || null;
  // Module-aware starter prompts on the empty state (#480); the Shell already holds
  // this query, so the cache is warm.
  const modules = useQuery({ queryKey: ["modules"], queryFn: () => api.modules(), staleTime: 30_000 });

  // The model this chat will actually use (the per-chat choice, else the core default). Check
  // its capabilities so we can warn when it can't use tools (local only) or can't see images
  // (local or hosted — #633: the gateway reports hosted capabilities too, via LiteLLM).
  const effectiveModel = model ?? llmPrefs.data?.global_default ?? null;
  const effectiveIsLocal = Boolean(effectiveModel) && !isHostedModelId(effectiveModel!);
  const modelDetails = useQuery({
    queryKey: ["modelDetails", effectiveModel],
    queryFn: () => api.modelDetails(effectiveModel!),
    enabled: Boolean(effectiveModel),
  });
  const caps = modelDetails.data?.capabilities ?? [];
  // Only warn when the runtime/gateway actually reported capabilities and the one we're
  // checking isn't among them — an empty list means "unknown", not "unsupported".
  const capsKnown = caps.length > 0;
  const toolless = effectiveIsLocal && capsKnown && !caps.includes("tools");
  const hasImageAttachment = attachments.some((a) => a.kind.startsWith("image/"));
  const visionUnsupported = hasImageAttachment && capsKnown && !caps.includes("vision");

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
      const nowPinned = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
      pinnedRef.current = nowPinned;
      setPinned(nowPinned); // no-op re-render while the value is unchanged
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);
  useEffect(() => {
    const el = scrollRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [chat.segments, chat.pendingUser, chat.awaiting, history.data]);

  // Mirror a draft-review pause (ADR-0085, #563) into the right panel: while `awaitingDraft` is set,
  // keep the composed email in the split-pane for Confirm/Decline — open it when the pause begins,
  // and **re-open it if the panel is dismissed while the draft is still pending** (the panel's
  // generic Close / backdrop must not strand a live draft; the decision is Confirm/Decline, and a
  // fresh chat message is still the escape hatch). Subscribing to the panel's top view is what makes
  // a manual close reactive. Reset to *exactly* the draft (close-then-open) rather than pushing, so
  // repeated re-opens can't grow the stack. Driven off the persisted `awaitingDraft`, so a reload
  // mid-review re-opens the pane on mount. The stores stay decoupled — this effect is the seam.
  const panelTop = usePanel((s) => s.stack[s.stack.length - 1]?.view ?? null);
  useEffect(() => {
    const panel = usePanel.getState();
    if (chat.awaitingDraft) {
      if (panelTop !== "email-draft") {
        panel.close();
        panel.open("email-draft", chat.awaitingDraft.draft, "Review email");
      }
    } else if (panelTop === "email-draft") {
      panel.back();
    }
  }, [chat.awaitingDraft, panelTop]);

  // The document pane (#541, ADR-0101): when the turn writes a document, show it beside the
  // chat. Unlike the draft review above, this one is **dismissible** — a document is an
  // artifact to watch, not a decision that must be resolved — so a close sets `dismissed`
  // rather than being re-opened, and only a write to a *different* document opens it again.
  // Best-effort throughout: the pane never gates the turn. A draft review outranks it (that
  // pane is waiting on the user), so it yields while one is up.
  const liveDocument = chat.liveDocument;
  useEffect(() => {
    const panel = usePanel.getState();
    const show = liveDocument !== null && !liveDocument.dismissed && !chat.awaitingDraft;
    if (show) {
      if (panelTop !== "document") {
        panel.close();
        panel.open("document", liveDocument, liveDocument.title || "Document");
      } else {
        panel.replace(liveDocument); // same pane, later frame of the same write
      }
    } else if (panelTop === "document") {
      panel.back();
    }
  }, [liveDocument, chat.awaitingDraft, panelTop]);

  // Re-attach to an in-flight turn after a reload / reconnect / app-resume (#376): the turn
  // keeps running server-side, so recover it instead of leaving a stale spinner or showing a
  // network error. Fires on mount, when the tab becomes visible again, and when the network
  // returns; `resumeIfActive` is a no-op when a stream is already live. An idle probe that
  // never confirms a run gives up silently (#477) — it never reaches the banner below.
  const onSessionSynced = useCallback(async () => {
    await queryClient.refetchQueries({ queryKey: ["session", chat.sessionId] });
    void queryClient.invalidateQueries({ queryKey: ["sessions"] });
  }, [chat.sessionId, queryClient]);
  const resumeIfActive = chat.resumeIfActive;
  useEffect(() => {
    const resume = () => {
      if (document.visibilityState === "visible") void resumeIfActive(onSessionSynced);
    };
    // `online` specifically is a connectivity signal (#477): if a probe is already
    // sleeping in backoff, this resets its attempt budget instead of just being ignored.
    const onOnline = () => void resumeIfActive(onSessionSynced, true);
    resume();
    document.addEventListener("visibilitychange", resume);
    window.addEventListener("online", onOnline);
    return () => {
      document.removeEventListener("visibilitychange", resume);
      window.removeEventListener("online", onOnline);
    };
  }, [resumeIfActive, onSessionSynced]);

  const send = () => {
    const text = chat.draft.trim();
    // The connection gate also covers Enter-to-send, not just the disabled button (#494).
    if (!text || chat.streaming || connectionLost) return;
    const sent = attachments;
    setAttachments([]); // chat.send clears the draft itself
    pin();
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
  // While a clarifying question is pending, the answer input is the focus — hide Edit/Regenerate.
  const turnControlsVisible =
    !chat.streaming &&
    !showPending &&
    editingIdx === null &&
    chat.awaiting === null &&
    chat.awaitingDraft === null;

  // What a pending mid-history edit would discard: every message after the edited one — the
  // answer to it and every turn since (#552).
  const discardedByEdit = confirmingEdit === null ? 0 : messages.length - confirmingEdit.idx - 1;

  const onTurnDone = async () => {
    await queryClient.refetchQueries({ queryKey: ["session", chat.sessionId] });
    void queryClient.invalidateQueries({ queryKey: ["sessions"] });
  };

  // Regenerate: optimistically drop the stale answer (everything after the last user turn),
  // then stream a fresh one. The server truncates the same tail before re-answering (#302).
  const regenerate = () => {
    if (chat.streaming || lastUserIdx < 0 || connectionLost) return;
    queryClient.setQueryData<MessageRecord[]>(["session", chat.sessionId], (old) =>
      (old ?? []).slice(0, lastUserIdx + 1),
    );
    pin();
    void chat.regenerate(model, onTurnDone);
  };

  const cancelEdit = () => {
    setEditingIdx(null);
    setEditText("");
  };

  // Apply an edit to the user message at `idx`: optimistically show the corrected text and
  // drop everything after it, then stream the new answer. The server revises + truncates the
  // same tail, keyed by the message's own id rather than its position (#552).
  const applyEdit = (idx: number, content: string) => {
    const target = messages[idx];
    if (!target) return;
    setEditingIdx(null);
    setEditText("");
    queryClient.setQueryData<MessageRecord[]>(["session", chat.sessionId], (old) => {
      const trimmed = (old ?? []).slice(0, idx + 1);
      const last = trimmed[trimmed.length - 1];
      if (last) trimmed[trimmed.length - 1] = { ...last, content };
      return trimmed;
    });
    pin();
    void chat.editAndRerun(content, model, onTurnDone, target.id);
  };

  // Save an edited user message. Editing the last one only replaces the answer that is about
  // to be regenerated, so it goes straight through (#302's behavior, unchanged). Editing
  // further back discards real turns — confirm with the count before any of it is lost (#552).
  const saveEdit = () => {
    const content = editText.trim();
    const idx = editingIdx;
    if (!content || idx === null || chat.streaming || connectionLost) return;
    if (idx < lastUserIdx) {
      setConfirmingEdit({ idx, content });
      return;
    }
    applyEdit(idx, content);
  };

  // A starter chip fills the composer and hands over the caret — it never sends on the
  // user's behalf. Openers ending in a space leave the cursor waiting mid-sentence.
  const pickStarter = (prompt: string) => {
    chat.setDraft(prompt);
    const el = composerRef.current;
    if (el) {
      el.focus();
      requestAnimationFrame(() => el.setSelectionRange(el.value.length, el.value.length));
    }
  };

  // Paste & drag-drop attachments (#489): every route lands on the same upload endpoint
  // the AttachMenu picker uses, so the server's 413/415 size/type messages stay the one
  // source of truth — here they surface as a toast instead of the picker's inline line.
  const uploadFiles = useCallback((files: File[]) => {
    for (const file of files) {
      const id = ++uploadSeq.current;
      setUploading((prev) => [...prev, { id, name: file.name || "pasted file" }]);
      void api
        .uploadAttachment(file)
        .then((res) =>
          setAttachments((prev) => [
            ...prev,
            { att_id: res.att_id, source: "file", kind: res.kind, title: res.title },
          ]),
        )
        .catch((err: unknown) =>
          toast.error(err instanceof ApiError ? err.detail : "Could not attach the file."),
        )
        .finally(() => setUploading((prev) => prev.filter((u) => u.id !== id)));
    }
  }, []);

  // Share target (#493): the OS share sheet's payload, stashed by the service worker
  // (src/sw.ts) and handed off via the ?share=1 redirect. Prefills the composer with the
  // text/url (appended to any draft already in progress rather than clobbering it) and
  // uploads any shared file through the same path a paste/drop does (#489) — never sends on
  // the operator's behalf, same as a starter chip. Runs once: a share deep-link is always a
  // fresh external trigger, never re-issued from within an already-mounted chat screen.
  const shareHandledRef = useRef(false);
  useEffect(() => {
    if (searchParams.get("share") !== "1" || shareHandledRef.current) return;
    shareHandledRef.current = true;
    void (async () => {
      try {
        const cache = await caches.open(SHARE_CACHE);
        const metaResp = await cache.match(SHARE_META_KEY);
        if (metaResp) {
          const meta = (await metaResp.json()) as ShareMeta;
          const shared = [meta.text, meta.url].filter(Boolean).join("\n");
          if (shared) chat.setDraft(chat.draft ? `${chat.draft}\n${shared}` : shared);
          if (meta.hasFile) {
            const fileResp = await cache.match(SHARE_FILE_KEY);
            if (fileResp) {
              const blob = await fileResp.blob();
              const name = decodeURIComponent(
                fileResp.headers.get(SHARE_FILE_NAME_HEADER) ?? "shared-file",
              );
              uploadFiles([new File([blob], name, { type: blob.type })]);
            }
          }
        }
        await cache.delete(SHARE_META_KEY);
        await cache.delete(SHARE_FILE_KEY);
      } catch {
        toast.error("Could not load the shared content.");
      } finally {
        setSearchParams(
          (prev) => {
            const next = new URLSearchParams(prev);
            next.delete("share");
            return next;
          },
          { replace: true },
        );
      }
    })();
    // chat.draft/setDraft and uploadFiles are read fresh inside the callback, not tracked as
    // reactive deps — this effect's trigger is the presence of the param, nothing else.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  // Only real file drags count — a text selection or an in-app drag must neither show
  // the overlay nor swallow the drop.
  const isFileDrag = (e: DragEvent<HTMLDivElement>) => e.dataTransfer.types.includes("Files");
  // Suppress-under-overlay (#511): a Sheet's backdrop blocks clicks but not native drag
  // events, so a drag used to pop the hint and upload *underneath* the layer the user
  // was looking at. While any aria-modal overlay is open, the drop surface is inert.
  // DOM-checked rather than lifted state so overlays this screen doesn't own (the
  // model sheet, a Confirm, a module dialog) count too.
  const overlayOpen = () => document.querySelector('[aria-modal="true"]') != null;
  const onDragEnter = (e: DragEvent<HTMLDivElement>) => {
    if (!isFileDrag(e) || overlayOpen()) return;
    dragDepth.current += 1;
    setDragActive(true);
  };
  const onDragOver = (e: DragEvent<HTMLDivElement>) => {
    if (!isFileDrag(e)) return;
    // Always claim the drop target — otherwise the browser navigates to the file on
    // drop. Suppression is enforced in onDrop; the cursor just signals it here.
    e.preventDefault();
    if (overlayOpen()) e.dataTransfer.dropEffect = "none";
  };
  const onDragLeave = (e: DragEvent<HTMLDivElement>) => {
    if (!isFileDrag(e)) return;
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragActive(false);
  };
  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    if (!isFileDrag(e)) return;
    e.preventDefault(); // swallow it even when suppressed — never let the browser open the file
    dragDepth.current = 0;
    setDragActive(false);
    if (overlayOpen()) return; // #511: no upload beneath an open overlay
    uploadFiles(Array.from(e.dataTransfer.files));
  };

  return (
    <div
      className="relative flex h-full flex-col"
      onDragEnter={onDragEnter}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      {/* drop hint (#489) — pointer-events-none so the drop lands on the container */}
      {dragActive && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 z-40 flex items-center justify-center bg-canvas/70"
        >
          <div className="flex items-center gap-2 rounded-(--radius-card) border-2 border-dashed border-accent bg-surface px-5 py-3 font-serif text-[15px] text-ink shadow-(--ep-shadow)">
            <Paperclip size={16} className="text-accent" />
            Drop to attach
          </div>
        </div>
      )}
      {/* chat header row: nav controls · the open conversation's title (#480) · model */}
      <div className="flex items-center gap-2 border-b border-edge px-4 py-2">
        <div className="flex shrink-0 items-center gap-2">
          <Tooltip label="Conversations" side="bottom">
            <button
              onClick={() => setSessionsOpen(true)}
              aria-label={
                chat.unseenFinished.size > 0 ? "Conversations (unseen answer)" : "Conversations"
              }
              className="relative rounded-md p-1.5 text-ink-dim hover:bg-surface-2 hover:text-ink"
            >
              <History size={18} />
              {/* A turn finished elsewhere while unseen (#492) — cleared once that session opens. */}
              {chat.unseenFinished.size > 0 && (
                <span className="absolute right-1 top-1">
                  <Dot tone="accent" />
                </span>
              )}
            </button>
          </Tooltip>
          <Tooltip label="New chat" side="bottom">
            <button
              onClick={() => chat.newSession()}
              aria-label="New chat"
              className="rounded-md p-1.5 text-ink-dim hover:bg-surface-2 hover:text-ink"
            >
              <SquarePen size={18} />
            </button>
          </Tooltip>
        </div>
        <h1
          className={cn(
            "min-w-0 flex-1 truncate text-center font-serif text-sm",
            sessionTitle ? "text-ink" : "italic text-ink-faint",
          )}
        >
          {sessionTitle ?? "New conversation"}
        </h1>
        <ModelPicker />
      </div>

      {/* transcript */}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        <div className="mx-auto flex max-w-2xl flex-col gap-5">
          {firstRun && <Welcome />}
          {!firstRun && messages.length === 0 && !chat.pendingUser && (
            <EmptyState quote={dayQuote()}>
              <p className="text-xs text-ink-faint">a new conversation — it will remember</p>
              <StarterPrompts modules={modules.data ?? []} onPick={pickStarter} />
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
                    <Button
                      variant="primary"
                      onClick={saveEdit}
                      disabled={!editText.trim() || connectionLost}
                    >
                      Resend
                    </Button>
                  </div>
                </div>
              ) : (
                <div key={i} className="group/msg flex flex-col items-end gap-1">
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
                  {turnControlsVisible && (
                    <button
                      aria-label="Edit message"
                      onClick={() => {
                        setEditingIdx(i);
                        setEditText(message.content);
                      }}
                      // Always at hand on the latest ask; earlier turns reveal it on hover or
                      // keyboard focus, to keep the transcript quiet (#480's Copy pattern).
                      className={cn(
                        "flex items-center gap-1 text-[11px] text-ink-faint hover:text-ink",
                        i !== lastUserIdx &&
                          "opacity-0 transition-opacity group-hover/msg:opacity-100 focus-visible:opacity-100",
                      )}
                    >
                      <Pencil size={12} /> Edit
                    </button>
                  )}
                </div>
              )
            ) : (
              <div key={i} className="group/msg">
                <AssistantBlock
                  text={message.content}
                  timeline={activityTimeline(message.activity)}
                  streaming={false}
                  entityRefs={message.entity_refs}
                />
                {(message.content !== "" || (i === lastAssistantIdx && turnControlsVisible)) && (
                  <div className="mt-1 ml-7 flex items-center gap-3">
                    {message.content !== "" && (
                      <CopyMessage
                        text={message.content}
                        // Always at hand on the latest answer; earlier turns reveal it on
                        // hover or keyboard focus to keep the transcript quiet.
                        className={cn(
                          i !== lastAssistantIdx &&
                            "opacity-0 transition-opacity group-hover/msg:opacity-100 focus-visible:opacity-100",
                        )}
                      />
                    )}
                    {i === lastAssistantIdx && turnControlsVisible && (
                      <button
                        aria-label="Regenerate response"
                        onClick={regenerate}
                        disabled={connectionLost}
                        className="flex items-center gap-1 text-[11px] text-ink-faint hover:text-ink disabled:opacity-50"
                      >
                        <RefreshCw size={12} /> Regenerate
                      </button>
                    )}
                  </div>
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
          {chat.awaiting && (
            <div className="ep-settle">
              <AskUserPrompt
                question={chat.awaiting.question}
                onSubmit={(answer) => {
                  pin();
                  void chat.resume(answer, onTurnDone);
                }}
              />
            </div>
          )}
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
                <div className="flex items-center justify-between gap-3">
                  <p className="text-danger">{chat.error}</p>
                  {chat.reconnectable && (
                    <Button
                      variant="ghost"
                      className="shrink-0 gap-1.5 text-xs"
                      onClick={() => void chat.reconnect(onSessionSynced)}
                    >
                      <RefreshCw size={13} />
                      Reconnect
                    </Button>
                  )}
                </div>
              )}
            </Card>
          )}
          <div className="h-2" />
        </div>
        {/* Scrolled up (reading back, or during a long stream): one tap returns to the
            tail and re-pins the view. Sticky inside the scroller, so it floats at the
            scrollport's bottom edge without an extra positioning wrapper (#480). */}
        {!pinned && (
          <button
            onClick={() => {
              const el = scrollRef.current;
              if (el) el.scrollTop = el.scrollHeight;
              pin();
            }}
            aria-label="Jump to latest"
            className={cn(
              "ep-settle sticky bottom-1 mx-auto flex items-center justify-center",
              "rounded-full border border-edge bg-surface p-2 text-ink-dim shadow-(--ep-shadow)",
              "transition-colors hover:border-accent hover:text-accent-strong",
            )}
          >
            <ArrowDown size={16} />
          </button>
        )}
      </div>

      {/* composer */}
      <div className="border-t border-edge px-4 py-3 pb-safe">
        {connectionLost && (
          <div className="mx-auto mb-2 flex max-w-2xl items-center gap-1.5 rounded-full border border-edge bg-surface-2 px-3 py-1 text-[11px] text-ink-dim">
            <WifiOff size={12} className="shrink-0 text-ink-faint" />
            <span>can't send right now — your draft is kept until epicurus is reachable.</span>
          </div>
        )}
        {toolless && (
          <div className="mx-auto mb-2 flex max-w-2xl items-center gap-1.5 rounded-full border border-edge bg-surface-2 px-3 py-1 text-[11px] text-ink-dim">
            <Wrench size={12} className="shrink-0 text-ink-faint" />
            <span>
              <span className="font-medium text-ink">{effectiveModel}</span> can't use tools — it
              can only chat (no calendar, files, or other actions).
            </span>
          </div>
        )}
        {visionUnsupported && (
          <div className="mx-auto mb-2 flex max-w-2xl items-center gap-1.5 rounded-full border border-edge bg-surface-2 px-3 py-1 text-[11px] text-ink-dim">
            <Eye size={12} className="shrink-0 text-ink-faint" />
            <span>
              <span className="font-medium text-ink">{effectiveModel}</span> can't see images —
              switch to a vision-capable model to use the attached image.
            </span>
          </div>
        )}
        <SuggestionBubble />
        {(attachments.length > 0 || uploading.length > 0) && (
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
            {uploading.map((u) => (
              <PendingAttachmentPill key={u.id} name={u.name} />
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
            onPaste={(e) => {
              // A clipboard with files (screenshot, copied file) attaches them (#489);
              // plain text pastes flow through untouched.
              const files = Array.from(e.clipboardData?.files ?? []);
              if (files.length === 0) return;
              e.preventDefault(); // don't also paste the file's name as text
              uploadFiles(files);
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
              disabled={!chat.draft.trim() || connectionLost}
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
      {/* Editing back in the history rewrites the conversation from there — say how much of it
          goes before anything is lost (#552). Cancelling leaves the composer open, draft intact. */}
      <Confirm
        open={confirmingEdit !== null}
        message={discardedByEdit === 1
          ? "Resending this message removes the 1 later message in this conversation."
          : `Resending this message removes the ${discardedByEdit} later messages in this conversation.`}
        confirmLabel="Resend"
        danger
        onConfirm={() => {
          // Re-check streaming/connectionLost at click time, same guard saveEdit applies
          // before ever opening this dialog (#660) — a run can start (or the connection can
          // drop) in the time the dialog sits open, and applying the trim regardless would
          // show a truncation the server was never asked to make. Blocked exactly like
          // Cancel: the inline editor stays open with the draft intact to retry.
          if (confirmingEdit && !chat.streaming && !connectionLost) {
            applyEdit(confirmingEdit.idx, confirmingEdit.content);
          }
          setConfirmingEdit(null);
        }}
        onCancel={() => setConfirmingEdit(null)}
      />
    </div>
  );
}
