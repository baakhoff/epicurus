/**
 * Chat — the main surface. Streams agent turns over SSE (tokens settle in
 * behind a pulsing caret, tool calls surface as live chips), grounds every
 * session in cross-chat memory via session_id, and lets you switch the model
 * mid-conversation.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronDown,
  CloudMoon,
  History,
  SquarePen,
  Square,
  SendHorizonal,
  Trash2,
  Wrench,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import {
  EntityRefChip,
  EntityRefsContext,
  inlinedRefIds,
  refsById,
} from "@/components/EntityRef";
import { Markdown } from "@/components/Markdown";
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
import { api } from "@/lib/api";
import type { EntityRef } from "@/lib/contracts";
import { relativeTime, PROVIDER_MODEL_HINTS } from "@/lib/format";
import { useChat, type ChatSegment } from "@/stores/chat";
import { useDownloads } from "@/stores/downloads";
import { usePrefs } from "@/stores/prefs";

const QUOTE =
  "It is not what we have but what we enjoy that constitutes our abundance.";

/* ── tool chip ──────────────────────────────────────────────────────────── */

function ToolChip({ segment }: { segment: Extract<ChatSegment, { kind: "tool" }> }) {
  const [open, setOpen] = useState(false);
  const { run } = segment;
  return (
    <div className="my-2">
      <button
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition-colors",
          run.status === "running" && "border-accent/40 text-accent-strong",
          run.status === "ok" && "border-ok/40 text-ok",
          run.status === "error" && "border-danger/40 text-danger",
        )}
      >
        <Wrench size={12} />
        {run.tool}
        {run.status === "running" ? (
          <Spinner className="size-3" />
        ) : run.status === "ok" ? (
          <Check size={12} />
        ) : (
          <X size={12} />
        )}
      </button>
      {open && run.detail && (
        <pre className="mt-1.5 max-h-40 overflow-auto rounded-(--radius-field) border border-edge bg-surface-2 p-2.5 font-mono text-[11px] leading-relaxed text-ink-dim">
          {run.detail}
        </pre>
      )}
    </div>
  );
}

/* ── live (streaming) assistant turn ────────────────────────────────────── */

function LiveTurn() {
  const segments = useChat((s) => s.segments);
  const streaming = useChat((s) => s.streaming);
  if (segments.length === 0 && !streaming) return null;
  return (
    <div className="ep-settle">
      <AssistantBlock segments={segments} streaming={streaming} />
    </div>
  );
}

function AssistantBlock({
  segments,
  streaming,
  entityRefs = [],
}: {
  segments: ChatSegment[];
  streaming: boolean;
  entityRefs?: EntityRef[];
}) {
  const refsMap = useMemo(() => refsById(entityRefs), [entityRefs]);
  const text = useMemo(
    () => segments.map((s) => (s.kind === "text" ? s.text : "")).join("\n"),
    [segments],
  );
  // Refs not already linked inline get a chip row beneath the message, so every
  // referenced entity surfaces exactly once (ADR-0019).
  const rowRefs = useMemo(() => {
    const inlined = inlinedRefIds(text);
    return entityRefs.filter((ref) => !inlined.has(ref.ref_id));
  }, [entityRefs, text]);

  return (
    <div className="flex gap-3">
      <div className="mt-1.5 font-serif text-[15px] leading-none text-accent select-none">ε</div>
      <div className="min-w-0 flex-1">
        <EntityRefsContext.Provider value={refsMap}>
          {segments.map((segment, i) =>
            segment.kind === "text" ? (
              <Markdown key={i}>{segment.text}</Markdown>
            ) : (
              <ToolChip key={i} segment={segment} />
            ),
          )}
        </EntityRefsContext.Provider>
        {streaming && (
          <span className="ep-caret ml-0.5 inline-block h-4 w-2 translate-y-0.5 rounded-[2px] bg-accent" />
        )}
        {rowRefs.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {rowRefs.map((ref) => (
              <EntityRefChip key={ref.ref_id} entref={ref} />
            ))}
          </div>
        )}
      </div>
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
  const models = useQuery({ queryKey: ["models"], queryFn: api.models, enabled: open });
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
  onPick,
}: {
  label: string;
  active: boolean;
  loaded?: boolean;
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
      <span className="flex items-center gap-2">
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

/* ── the screen ─────────────────────────────────────────────────────────── */

export function ChatScreen() {
  const queryClient = useQueryClient();
  const chat = useChat();
  const model = usePrefs((s) => s.model);
  const [draft, setDraft] = useState("");
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);

  const history = useQuery({
    queryKey: ["session", chat.sessionId],
    queryFn: () => api.sessionMessages(chat.sessionId),
  });
  const models = useQuery({ queryKey: ["models"], queryFn: api.models });
  const providers = useQuery({ queryKey: ["providers"], queryFn: api.providers });

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
    const text = draft.trim();
    if (!text || chat.streaming) return;
    setDraft("");
    pinnedRef.current = true;
    void chat.send(text, model, async () => {
      await queryClient.refetchQueries({ queryKey: ["session", chat.sessionId] });
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
    });
  };

  // While a turn streams, history already contains the just-sent user message —
  // suppress the optimistic copy once the server history catches up.
  const messages = history.data ?? [];
  const showPending =
    chat.pendingUser !== null &&
    (chat.streaming || messages[messages.length - 1]?.content !== chat.pendingUser);

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
              <div key={i} className="flex justify-end">
                <div className="max-w-[85%] rounded-2xl rounded-br-md bg-user-bubble px-4 py-2.5 text-[15px] leading-relaxed whitespace-pre-wrap">
                  {message.content}
                </div>
              </div>
            ) : (
              <AssistantBlock
                key={i}
                segments={[{ kind: "text", text: message.content }]}
                streaming={false}
                entityRefs={message.entity_refs}
              />
            ),
          )}
          {showPending && (
            <div className="flex justify-end">
              <div className="max-w-[85%] rounded-2xl rounded-br-md bg-user-bubble px-4 py-2.5 text-[15px] leading-relaxed whitespace-pre-wrap">
                {chat.pendingUser}
              </div>
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
        <div className="mx-auto flex max-w-2xl items-end gap-2">
          <TextArea
            rows={1}
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
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
              disabled={!draft.trim()}
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
