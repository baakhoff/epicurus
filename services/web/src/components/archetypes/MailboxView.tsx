/**
 * MailboxView — the `mailbox` archetype (ADR-0087): a mail client rendered entirely by the
 * shell (the mail module ships zero markup). A labels rail selects a folder; the main pane
 * shows a paginated thread list, one open conversation, or a compose/reply form. Reads flow
 * through the generic module-page proxy (`?label=`/`?q=`/`?cursor=`/`?thread_id=`); messages
 * render through the shared `MailMessageView` (the same component the panel `email-reader`
 * uses); sends go through the gated, operator-only send proxy (never the agent — ADR-0085).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ChevronLeft, ChevronRight, Mail, PenSquare, Search, WifiOff } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { MailMessageView } from "@/components/MailMessageView";
import { Button, EmptyState, Select, Spinner, TextArea, TextInput, cn } from "@/components/ui";
import { api } from "@/lib/api";
import {
  MailboxListData,
  MailboxThreadData,
  type MailboxReply,
  type MailThreadData,
  type MailThreadSummary,
} from "@/lib/contracts";
import { useConnection } from "@/stores/connection";

/**
 * Optimistically flip a thread to read (#625) across whichever module-page query this is: a list
 * page (flip the matching thread row) or the open thread (flip every message's unread badge).
 * Shape-guarded so it's a no-op on any other cached page under the same key prefix.
 */
function optimisticallyMarkRead(data: unknown, threadId: string): unknown {
  if (!data || typeof data !== "object") return data;
  const d = data as { threads?: MailThreadSummary[]; thread?: MailThreadData };
  if (Array.isArray(d.threads)) {
    return {
      ...d,
      threads: d.threads.map((t) => (t.id === threadId ? { ...t, unread: false } : t)),
    };
  }
  if (d.thread && Array.isArray(d.thread.messages)) {
    return {
      ...d,
      thread: { ...d.thread, messages: d.thread.messages.map((m) => ({ ...m, unread: false })) },
    };
  }
  return data;
}

/** A raw provider date string shortened for a list row; falls back to the raw text. */
function shortDate(raw: string): string {
  if (!raw) return "";
  const parsed = new Date(raw);
  if (Number.isNaN(parsed.getTime())) return raw;
  const now = new Date();
  const sameYear = parsed.getFullYear() === now.getFullYear();
  return parsed.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
  });
}

/* ── labels rail ─────────────────────────────────────────────────────────── */

function LabelRail({
  labels,
  active,
  onSelect,
}: {
  labels: MailboxListData["labels"];
  active: string;
  onSelect: (id: string) => void;
}) {
  return (
    <nav
      aria-label="Mailbox folders"
      className="hidden w-44 shrink-0 flex-col gap-0.5 overflow-y-auto border-r border-edge p-2 sm:flex"
    >
      {labels.map((label) => (
        <button
          key={label.id}
          onClick={() => onSelect(label.id)}
          aria-current={label.id === active ? "page" : undefined}
          className={cn(
            "flex items-center justify-between gap-2 rounded-(--radius-field) px-2.5 py-1.5 text-sm transition-colors",
            label.id === active
              ? "bg-accent-dim text-accent-strong"
              : "text-ink-dim hover:bg-surface-2 hover:text-ink",
          )}
        >
          <span className="truncate">{label.title}</span>
          {typeof label.unread === "number" && label.unread > 0 && (
            <span className="shrink-0 rounded-full bg-surface-3 px-1.5 text-[11px] text-ink-dim">
              {label.unread}
            </span>
          )}
        </button>
      ))}
    </nav>
  );
}

/* ── thread list ─────────────────────────────────────────────────────────── */

function ThreadRow({ thread, onOpen }: { thread: MailThreadSummary; onOpen: () => void }) {
  return (
    <button
      onClick={onOpen}
      className="flex w-full flex-col gap-0.5 border-b border-edge px-4 py-2.5 text-left transition-colors hover:bg-surface-2"
    >
      <div className="flex items-center gap-2">
        {thread.unread && <span className="size-2 shrink-0 rounded-full bg-accent" aria-hidden />}
        <span
          className={cn(
            "min-w-0 flex-1 truncate text-sm",
            thread.unread ? "font-semibold text-ink" : "text-ink-dim",
          )}
        >
          {thread.sender || "(unknown sender)"}
        </span>
        {thread.message_count > 1 && (
          <span className="shrink-0 text-[11px] text-ink-faint">{thread.message_count}</span>
        )}
        <span className="shrink-0 text-[11px] text-ink-faint">{shortDate(thread.date)}</span>
      </div>
      <span className={cn("truncate text-sm", thread.unread ? "text-ink" : "text-ink-dim")}>
        {thread.subject}
      </span>
      {thread.snippet && <span className="truncate text-xs text-ink-faint">{thread.snippet}</span>}
    </button>
  );
}

/* ── compose / reply ─────────────────────────────────────────────────────── */

function ComposeForm({
  module,
  pageId,
  reply,
  onClose,
  onSent,
}: {
  module: string;
  pageId: string;
  reply?: MailboxReply | null;
  onClose: () => void;
  onSent: () => void;
}) {
  const [to, setTo] = useState(reply?.to ?? "");
  const [subject, setSubject] = useState(reply?.subject ?? "");
  const [body, setBody] = useState("");
  const [confirming, setConfirming] = useState(false);
  // Send is as send-adjacent as the chat composer, so gate it on the connection the same way (#530).
  const connectionLost = useConnection((s) => s.coreDown || !s.online);

  const send = useMutation({
    mutationFn: () =>
      api.sendMailboxMessage(
        module,
        pageId,
        reply
          ? { body, reply_to_message_id: reply.reply_to_message_id }
          : { body, to: to.trim(), subject },
      ),
    onSuccess: onSent,
  });

  const canSend = body.trim().length > 0 && (reply != null || to.trim().length > 0);
  const submit = () => {
    if (!canSend || connectionLost || send.isPending) return;
    setConfirming(true);
  };

  return (
    <div className="mx-auto flex h-full max-w-2xl flex-col p-4">
      <div className="mb-3 flex items-center gap-2">
        <button
          onClick={onClose}
          className="rounded-md p-1 text-ink-dim hover:bg-surface-2 hover:text-ink"
          aria-label="Discard"
        >
          <ArrowLeft size={16} />
        </button>
        <h2 className="font-serif text-base text-ink">{reply ? "Reply" : "New message"}</h2>
      </div>
      {reply?.reply_to_original && (
        <p className="mb-2 text-xs text-ink-dim">Replying to {reply.reply_to_original}</p>
      )}
      <div className="flex flex-1 flex-col gap-2 overflow-y-auto">
        {reply ? (
          <>
            <FieldRow label="To" value={reply.to} />
            <FieldRow label="Subject" value={reply.subject} />
          </>
        ) : (
          <>
            <TextInput
              type="email"
              value={to}
              onChange={(e) => setTo(e.target.value)}
              placeholder="To"
            />
            <TextInput
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              placeholder="Subject"
            />
          </>
        )}
        <TextArea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          placeholder="Write your message…"
          className="min-h-40 flex-1 leading-relaxed"
        />
      </div>
      {send.isError && (
        <p className="mt-2 text-sm text-danger">{(send.error as Error).message}</p>
      )}
      {connectionLost && (
        <p className="mt-2 flex items-center gap-1.5 text-[11px] text-ink-dim">
          <WifiOff size={12} className="shrink-0 text-ink-faint" />
          can&apos;t send right now — epicurus is unreachable.
        </p>
      )}
      <div className="mt-3 flex items-center gap-2">
        <Button
          variant="primary"
          busy={send.isPending}
          disabled={!canSend || connectionLost || send.isPending}
          onClick={submit}
        >
          Send
        </Button>
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
      </div>
      {confirming && (
        <ConfirmSend
          onCancel={() => setConfirming(false)}
          onConfirm={() => {
            setConfirming(false);
            send.mutate();
          }}
        />
      )}
    </div>
  );
}

function FieldRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-2 rounded-(--radius-field) border border-edge bg-surface-2 px-3 py-2 text-sm">
      <span className="text-ink-dim">{label}</span>
      <span className="min-w-0 flex-1 truncate text-ink">{value || "—"}</span>
    </div>
  );
}

/** The danger-action confirm the issue asks for: the operator is the send button (ADR-0087). */
function ConfirmSend({ onCancel, onConfirm }: { onCancel: () => void; onConfirm: () => void }) {
  return (
    <div
      className="fixed inset-0 z-60 flex items-center justify-center p-6"
      role="alertdialog"
      aria-modal="true"
      aria-label="Send message"
    >
      <div className="absolute inset-0 bg-black/55" onClick={onCancel} />
      <div className="relative w-full max-w-sm rounded-(--radius-card) border border-edge bg-surface p-4 shadow-(--ep-shadow)">
        <p className="text-sm text-ink">Send this message?</p>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="primary" onClick={onConfirm}>
            Send
          </Button>
        </div>
      </div>
    </div>
  );
}

/* ── the view ────────────────────────────────────────────────────────────── */

export function MailboxView({ module, pageId }: { module: string; pageId: string }) {
  const queryClient = useQueryClient();
  const [label, setLabel] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [submitted, setSubmitted] = useState("");
  // Cursor pagination (never offset): a stack of the tokens used per page — [null] is page 1,
  // Next pushes the server's next_cursor, Back pops. Reset whenever the label/query changes.
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [openThreadId, setOpenThreadId] = useState<string | null>(null);
  const [composing, setComposing] = useState(false);

  const cursor = cursors[cursors.length - 1];
  const resetPaging = useCallback(() => setCursors([null]), []);

  const listQuery = useQuery({
    queryKey: ["module-page", module, pageId, "list", label, submitted, cursor],
    queryFn: () => {
      const params: Record<string, string> = {};
      if (label) params.label = label;
      if (submitted) params.q = submitted;
      if (cursor) params.cursor = cursor;
      return api.modulePage(module, pageId, params);
    },
  });

  // Cache-first landing (ADR-0096, #623): the plain folder view (no search, first page) serves
  // from the module's local cache instantly, then this second read reconciles the provider delta
  // into the cache and swaps in the fresh list — new/changed messages and flag flips appear
  // without a manual refresh. Gated on the cached read succeeding first, so a cold cache does one
  // full sync (the list read) rather than two racing ones. Search / deeper pages skip it.
  const isLanding = !submitted && !cursor;
  const reconcileQuery = useQuery({
    queryKey: ["module-page", module, pageId, "reconcile", label],
    queryFn: () => {
      const params: Record<string, string> = { reconcile: "1" };
      if (label) params.label = label;
      return api.modulePage(module, pageId, params);
    },
    enabled: isLanding && listQuery.isSuccess,
  });

  // Prefer the reconciled data once it lands; until then paint the instant cached read.
  const listData = (isLanding && reconcileQuery.data) || listQuery.data;
  const list = useMemo(
    () => (listData ? MailboxListData.parse(listData) : null),
    [listData],
  );
  const activeLabel = label ?? list?.active_label ?? "INBOX";

  const threadQuery = useQuery({
    queryKey: ["module-page", module, pageId, "thread", openThreadId],
    queryFn: () => api.modulePage(module, pageId, { thread_id: openThreadId as string }),
    enabled: openThreadId != null,
  });
  const thread = useMemo(
    () => (threadQuery.data ? MailboxThreadData.parse(threadQuery.data).thread : null),
    [threadQuery.data],
  );

  const selectLabel = (id: string) => {
    setLabel(id);
    setSubmitted("");
    setSearch("");
    resetPaging();
    setOpenThreadId(null);
    setComposing(false);
  };
  const runSearch = () => {
    setSubmitted(search.trim());
    resetPaging();
    setOpenThreadId(null);
  };
  // Refresh both the open thread AND the list after an in-thread action (archive / trash /
  // mark), so a triaged message's list row isn't stale when the user goes Back — the broad
  // page prefix covers both queries (the same key `afterSent` invalidates).
  const refetchThread = useCallback(
    () => queryClient.invalidateQueries({ queryKey: ["module-page", module, pageId] }),
    [queryClient, module, pageId],
  );
  const afterSent = useCallback(() => {
    setComposing(false);
    void queryClient.invalidateQueries({ queryKey: ["module-page", module, pageId] });
  }, [queryClient, module, pageId]);

  // Mark a thread read on open (#625): flip the list row + message badges optimistically, mark at
  // the provider in the background (the module also writes the read state through to its cache),
  // and converge on settle. `markedRef` stops a re-render from re-firing for the same thread.
  const markedRef = useRef<Set<string>>(new Set());
  const { mutate: markThreadRead } = useMutation({
    mutationFn: (vars: { threadId: string; messageIds: string[] }) =>
      api.markMailboxThreadRead(module, pageId, {
        thread_id: vars.threadId,
        message_ids: vars.messageIds,
      }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["module-page", module, pageId] }),
  });

  useEffect(() => {
    if (!openThreadId || !thread || markedRef.current.has(openThreadId)) return;
    const unreadIds = thread.messages
      .filter((m) => m.unread && m.message_id)
      .map((m) => m.message_id);
    if (unreadIds.length === 0) return;
    markedRef.current.add(openThreadId);
    queryClient.setQueriesData({ queryKey: ["module-page", module, pageId] }, (data: unknown) =>
      optimisticallyMarkRead(data, openThreadId),
    );
    markThreadRead({ threadId: openThreadId, messageIds: unreadIds });
  }, [openThreadId, thread, module, pageId, queryClient, markThreadRead]);

  return (
    <div className="flex h-full min-h-0">
      <LabelRail labels={list?.labels ?? []} active={activeLabel} onSelect={selectLabel} />
      <div className="flex min-w-0 flex-1 flex-col">
        {/* toolbar: mobile label picker + search + compose */}
        <div className="flex items-center gap-2 border-b border-edge px-3 py-2">
          <Select
            size="sm"
            value={activeLabel}
            onChange={(e) => selectLabel(e.target.value)}
            aria-label="Mailbox folder"
            className="sm:hidden"
          >
            {(list?.labels ?? []).map((l) => (
              <option key={l.id} value={l.id}>
                {l.title}
              </option>
            ))}
          </Select>
          <div className="relative flex min-w-0 flex-1 items-center">
            <Search
              size={14}
              className="pointer-events-none absolute left-2.5 text-ink-faint"
            />
            <TextInput
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && runSearch()}
              placeholder="Search mail…"
              className="pl-8"
            />
          </div>
          <Button variant="outline" size="sm" onClick={() => setComposing(true)}>
            <PenSquare size={15} />
            <span className="hidden sm:inline">New message</span>
          </Button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {composing ? (
            <ComposeForm
              module={module}
              pageId={pageId}
              onClose={() => setComposing(false)}
              onSent={afterSent}
            />
          ) : openThreadId && thread ? (
            <ThreadPane
              module={module}
              pageId={pageId}
              thread={thread}
              loading={threadQuery.isLoading}
              onBack={() => setOpenThreadId(null)}
              onActed={refetchThread}
              onSent={() => {
                void refetchThread();
              }}
            />
          ) : openThreadId && threadQuery.isLoading ? (
            <div className="flex h-full items-center justify-center">
              <Spinner />
            </div>
          ) : openThreadId && threadQuery.isError ? (
            // A thread read can fail with a Gmail scope/rate-limit hint (#538/#557) the module
            // relays — surface it (not the silent list) and give a Back that clears the failed
            // id so a re-open refetches instead of being a no-op on unchanged state.
            <div className="flex h-full flex-col items-center justify-center gap-3 p-6">
              <EmptyState quote="Couldn't open that conversation.">
                <p className="text-sm text-ink-dim">{(threadQuery.error as Error).message}</p>
              </EmptyState>
              <Button variant="outline" onClick={() => setOpenThreadId(null)}>
                <ArrowLeft size={15} /> Back to list
              </Button>
            </div>
          ) : listQuery.isLoading ? (
            <div className="flex h-full items-center justify-center">
              <Spinner />
            </div>
          ) : listQuery.isError ? (
            <div className="flex h-full items-center justify-center p-6">
              <EmptyState quote="Couldn't reach your mail.">
                <p className="text-sm text-ink-dim">{(listQuery.error as Error).message}</p>
              </EmptyState>
            </div>
          ) : list && list.threads.length > 0 ? (
            <ThreadList
              list={list}
              onOpen={setOpenThreadId}
              onPrev={() => setCursors((c) => (c.length > 1 ? c.slice(0, -1) : c))}
              onNext={() =>
                list.next_cursor && setCursors((c) => [...c, list.next_cursor as string])
              }
              hasPrev={cursors.length > 1}
            />
          ) : (
            <div className="flex h-full items-center justify-center p-6">
              <EmptyState quote={submitted ? "No messages match your search." : "Nothing here."}>
                <p className="text-sm text-ink-dim">
                  <Mail size={14} className="mr-1 inline text-ink-faint" />
                  {submitted ? "Try a different search." : "This folder is empty."}
                </p>
              </EmptyState>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function ThreadList({
  list,
  onOpen,
  onPrev,
  onNext,
  hasPrev,
}: {
  list: MailboxListData;
  onOpen: (id: string) => void;
  onPrev: () => void;
  onNext: () => void;
  hasPrev: boolean;
}) {
  return (
    <div className="flex h-full flex-col">
      <div className="min-h-0 flex-1">
        {list.threads.map((thread) => (
          <ThreadRow key={thread.id} thread={thread} onOpen={() => onOpen(thread.id)} />
        ))}
      </div>
      {(hasPrev || list.next_cursor) && (
        <div className="flex items-center justify-between border-t border-edge px-4 py-2">
          <Button variant="ghost" size="sm" disabled={!hasPrev} onClick={onPrev}>
            <ChevronLeft size={15} /> Newer
          </Button>
          <Button variant="ghost" size="sm" disabled={!list.next_cursor} onClick={onNext}>
            Older <ChevronRight size={15} />
          </Button>
        </div>
      )}
    </div>
  );
}

function ThreadPane({
  module,
  pageId,
  thread,
  loading,
  onBack,
  onActed,
  onSent,
}: {
  module: string;
  pageId: string;
  thread: NonNullable<MailboxThreadData["thread"]>;
  loading: boolean;
  onBack: () => void;
  onActed: () => void | Promise<void>;
  onSent: () => void;
}) {
  const [replying, setReplying] = useState(false);
  const attachmentUrl = useCallback(
    (messageId: string, attachmentId: string) =>
      api.mailboxAttachmentUrl(module, pageId, messageId, attachmentId),
    [module, pageId],
  );

  if (replying && thread.reply) {
    return (
      <ComposeForm
        module={module}
        pageId={pageId}
        reply={thread.reply}
        onClose={() => setReplying(false)}
        onSent={() => {
          setReplying(false);
          onSent();
        }}
      />
    );
  }

  return (
    <div className="mx-auto max-w-3xl p-4">
      <div className="mb-3 flex items-center gap-2">
        <button
          onClick={onBack}
          className="rounded-md p-1 text-ink-dim hover:bg-surface-2 hover:text-ink"
          aria-label="Back to list"
        >
          <ArrowLeft size={16} />
        </button>
        <h2 className="min-w-0 flex-1 truncate font-serif text-lg text-ink">{thread.subject}</h2>
        {loading && <Spinner />}
      </div>
      <div className="flex flex-col gap-4">
        {thread.messages.map((message, i) => (
          <div
            key={message.message_id || i}
            className="rounded-(--radius-card) border border-edge bg-surface p-4"
          >
            <MailMessageView
              message={message}
              showSubject={false}
              attachmentUrl={attachmentUrl}
              onActed={onActed}
            />
          </div>
        ))}
      </div>
      {thread.reply && (
        <div className="mt-4">
          <Button variant="primary" onClick={() => setReplying(true)}>
            <Mail size={15} /> Reply
          </Button>
        </div>
      )}
    </div>
  );
}
