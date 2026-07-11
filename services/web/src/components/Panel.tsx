/**
 * The right-panel host (ADR-0018). A core-owned split-screen panel: a resizable
 * right column on wide screens, a bottom sheet on phones. It renders a **bounded,
 * core-defined** set of views (`entity-detail`, `email-reader`) from the data a
 * caller passes through the panel store — no module markup ever runs here.
 */
import { useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, WifiOff, X } from "lucide-react";
import { Fragment, useCallback, useEffect, useRef, useState, type PointerEvent } from "react";

import { CardLink } from "@/components/CardLink";
import { MailMessageView } from "@/components/MailMessageView";
import { Markdown } from "@/components/Markdown";
import { Button, Sheet } from "@/components/ui";
import { api } from "@/lib/api";
import { EmailDraft, EmailMessage, FileText, HoverCard } from "@/lib/contracts";
import { useChat } from "@/stores/chat";
import { useConnection } from "@/stores/connection";
import { usePanel, usePanelCurrent, usePanelDepth, type PanelEntry } from "@/stores/panel";

/** Whether a file name reads as markdown (rendered) vs. plain text (shown verbatim). */
function isMarkdown(name: string): boolean {
  return /\.(md|markdown|mdx)$/i.test(name);
}

/* ── views (core-defined vocabulary) ─────────────────────────────────────── */

function EntityDetailView({ payload }: { payload: unknown }) {
  const data = HoverCard.parse(payload);
  return (
    <div>
      <h3 className="font-serif text-lg text-ink">{data.title}</h3>
      {data.description && (
        <p className="mt-1 text-sm leading-relaxed text-ink-dim">{data.description}</p>
      )}
      {data.details.length > 0 && (
        <dl className="mt-4 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
          {data.details.map((detail, i) => (
            <Fragment key={i}>
              <dt className="text-ink-dim">{detail.label}</dt>
              <dd className="text-ink">{detail.value}</dd>
            </Fragment>
          ))}
        </dl>
      )}
      {data.href && (
        <CardLink
          href={data.href}
          className="mt-4 inline-flex items-center gap-1 text-sm text-accent-strong hover:underline"
        />
      )}
    </div>
  );
}

/**
 * The `email-reader` view (ADR-0024): one message shown through the shared `MailMessageView`
 * (ADR-0087) — the same renderer the mailbox page's thread pane uses, so the two never fork.
 * After a mark read/unread action it re-fetches the message and swaps it into the panel so the
 * toggle flips. No attachment links here — the panel reader doesn't fetch attachments (the
 * mailbox page does).
 */
function EmailReaderView({ payload }: { payload: unknown }) {
  const mail = EmailMessage.parse(payload);
  const replace = usePanel((s) => s.replace);
  const onActed = useCallback(async () => {
    replace(await api.readMailMessage(mail.module, mail.message_id));
  }, [replace, mail.module, mail.message_id]);
  return <MailMessageView message={mail} onActed={mail.message_id ? onActed : undefined} />;
}

/**
 * The `email-draft` view (ADR-0085, #563): a message the agent composed, shown for the operator
 * to **Confirm** (send) or **Decline** in the split-pane. Nothing was sent to compose this — the
 * agent cannot send on its own; the operator is the send button. Renders through the same message
 * shape as `email-reader`. Confirm is danger-styled and gated on the connection (#530); Esc
 * declines (the destructive path is never the default), and Decline takes initial focus.
 */
function EmailDraftView({ payload }: { payload: unknown }) {
  const draft = EmailDraft.parse(payload);
  const streaming = useChat((s) => s.streaming);
  const resolveDraft = useChat((s) => s.resolveDraft);
  const sessionId = useChat((s) => s.sessionId);
  const queryClient = useQueryClient();
  // The same connection gate as the composer + ask_user prompt (#494/#530): Confirm is as
  // send-adjacent as Send, so it fails the same way if fired while the core is unreachable.
  const connectionLost = useConnection((s) => s.coreDown || !s.online);
  const declineRef = useRef<HTMLButtonElement>(null);

  const onDone = useCallback(async () => {
    await queryClient.refetchQueries({ queryKey: ["session", sessionId] });
    void queryClient.invalidateQueries({ queryKey: ["sessions"] });
  }, [queryClient, sessionId]);

  const resolve = useCallback(
    (decision: "send" | "decline") => {
      if (streaming || (decision === "send" && connectionLost)) return;
      void resolveDraft(decision, onDone);
    },
    [streaming, connectionLost, resolveDraft, onDone],
  );

  // Shell dialog conventions (#487): focus the safe action (Decline) on open, and let Esc decline
  // — Esc must never send, so the destructive path stays opt-in. Capture-phase + stopPropagation
  // so it resolves this pane without also triggering any outer handler.
  useEffect(() => {
    declineRef.current?.focus();
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || streaming) return;
      e.stopPropagation();
      resolve("decline");
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [resolve, streaming]);

  return (
    <article>
      <p className="text-[11px] font-medium tracking-wide text-ink-faint uppercase">
        Review before sending
      </p>
      {draft.reply_to_original && (
        <p className="mt-1 text-xs text-ink-dim">Replying to {draft.reply_to_original}</p>
      )}
      <h3 className="mt-2 font-serif text-lg text-ink">{draft.subject}</h3>
      <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
        <dt className="text-ink-dim">To</dt>
        <dd className="text-ink">{draft.to}</dd>
        {draft.cc && (
          <>
            <dt className="text-ink-dim">Cc</dt>
            <dd className="text-ink">{draft.cc}</dd>
          </>
        )}
      </dl>
      <p className="mt-4 text-[15px] leading-relaxed whitespace-pre-wrap text-ink">{draft.body}</p>
      <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-edge pt-4">
        <Button
          variant="danger"
          disabled={streaming || connectionLost}
          onClick={() => resolve("send")}
        >
          Confirm &amp; send
        </Button>
        <Button ref={declineRef} variant="outline" disabled={streaming} onClick={() => resolve("decline")}>
          Decline
        </Button>
      </div>
      {connectionLost && (
        <p className="mt-2 flex items-center gap-1.5 text-[11px] text-ink-dim">
          <WifiOff size={12} className="shrink-0 text-ink-faint" />
          can&apos;t send right now — the draft is kept until epicurus is reachable.
        </p>
      )}
    </article>
  );
}

/**
 * The `doc-reader` view (#KB-refactor, req 6): a file opened from the Files browser, read
 * in the split-screen panel — markdown rendered, anything else shown verbatim.
 */
function DocReaderView({ payload }: { payload: unknown }) {
  const file = FileText.parse(payload);
  return (
    <article>
      <p className="mb-3 truncate font-mono text-xs text-ink-faint" title={file.path}>
        {file.path}
      </p>
      {isMarkdown(file.name) ? (
        <Markdown>{file.content}</Markdown>
      ) : (
        <pre className="overflow-x-auto whitespace-pre-wrap font-mono text-[13px] leading-relaxed text-ink">
          {file.content}
        </pre>
      )}
    </article>
  );
}

function PanelBody({ entry }: { entry: PanelEntry }) {
  switch (entry.view) {
    case "entity-detail":
      return <EntityDetailView payload={entry.payload} />;
    case "email-reader":
      return <EmailReaderView payload={entry.payload} />;
    case "email-draft":
      return <EmailDraftView payload={entry.payload} />;
    case "doc-reader":
      return <DocReaderView payload={entry.payload} />;
    default:
      return null;
  }
}

/* ── hosts ───────────────────────────────────────────────────────────────── */

const MIN_WIDTH = 320;
const MAX_WIDTH = 640;

function DesktopPanel() {
  const current = usePanelCurrent();
  const depth = usePanelDepth();
  const back = usePanel((s) => s.back);
  const close = usePanel((s) => s.close);
  const [width, setWidth] = useState(384);
  const dragging = useRef(false);

  const onPointerDown = useCallback((e: PointerEvent<HTMLDivElement>) => {
    dragging.current = true;
    e.currentTarget.setPointerCapture(e.pointerId);
  }, []);
  const onPointerMove = useCallback((e: PointerEvent<HTMLDivElement>) => {
    if (!dragging.current) return;
    const next = window.innerWidth - e.clientX;
    setWidth(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, next)));
  }, []);
  const onPointerUp = useCallback((e: PointerEvent<HTMLDivElement>) => {
    dragging.current = false;
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* pointer already released */
    }
  }, []);

  if (!current) return null;
  return (
    <aside className="hidden shrink-0 sm:flex" style={{ width }} aria-label="Detail panel">
      <div
        role="separator"
        aria-orientation="vertical"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        className="w-1 shrink-0 cursor-col-resize bg-edge transition-colors hover:bg-accent/40"
      />
      <div className="flex min-w-0 flex-1 flex-col border-l border-edge">
        <header className="flex items-center gap-2 border-b border-edge px-4 py-2.5">
          {depth > 1 && (
            <button
              onClick={back}
              aria-label="Back"
              className="rounded-md p-1 text-ink-dim hover:bg-surface-2 hover:text-ink"
            >
              <ChevronLeft size={16} />
            </button>
          )}
          <h2 className="min-w-0 flex-1 truncate font-serif text-base text-ink">
            {current.title || "Details"}
          </h2>
          <button
            onClick={close}
            aria-label="Close panel"
            className="rounded-md p-1 text-ink-dim hover:bg-surface-2 hover:text-ink"
          >
            <X size={16} />
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <PanelBody entry={current} />
        </div>
      </div>
    </aside>
  );
}

function MobilePanel() {
  const current = usePanelCurrent();
  const depth = usePanelDepth();
  const back = usePanel((s) => s.back);
  const close = usePanel((s) => s.close);
  return (
    <Sheet open={current !== null} onClose={close} title={current?.title || "Details"}>
      {current && (
        <>
          {depth > 1 && (
            <button
              onClick={back}
              className="mb-3 inline-flex items-center gap-1 text-sm text-ink-dim hover:text-ink"
            >
              <ChevronLeft size={15} /> back
            </button>
          )}
          <PanelBody entry={current} />
        </>
      )}
    </Sheet>
  );
}

/** Mounts both hosts; CSS shows the right one for the viewport. */
export function PanelHost() {
  return (
    <>
      <DesktopPanel />
      <div className="sm:hidden">
        <MobilePanel />
      </div>
    </>
  );
}
