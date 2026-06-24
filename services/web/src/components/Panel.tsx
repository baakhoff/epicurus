/**
 * The right-panel host (ADR-0018). A core-owned split-screen panel: a resizable
 * right column on wide screens, a bottom sheet on phones. It renders a **bounded,
 * core-defined** set of views (`entity-detail`, `email-reader`) from the data a
 * caller passes through the panel store — no module markup ever runs here.
 */
import { ChevronLeft, X } from "lucide-react";
import { createElement, Fragment, useCallback, useRef, useState, type PointerEvent } from "react";

import { CardLink } from "@/components/CardLink";
import { Markdown } from "@/components/Markdown";
import { Button, Sheet } from "@/components/ui";
import { api } from "@/lib/api";
import { EmailMessage, FileText, HoverCard, type BoardAction } from "@/lib/contracts";
import { moduleIcon } from "@/lib/icons";
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
 * One tool-backed action on the open email (ADR-0024) — the Mark as read / unread toggle.
 * Invokes the module's MCP tool through the core proxy, then re-fetches the message and
 * swaps it into the panel so the toggle flips to its opposite. Plain local state (not
 * react-query) keeps the panel decoupled from the query client.
 */
function MailActionButton({
  module,
  messageId,
  action,
}: {
  module: string;
  messageId: string;
  action: BoardAction;
}) {
  const replace = usePanel((s) => s.replace);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await api.invokeModuleTool(module, action.tool, action.args);
      replace(await api.readMailMessage(module, messageId));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Action failed");
      setBusy(false);
    }
  }, [module, messageId, action.tool, action.args, replace]);

  return (
    <>
      <Button variant="outline" busy={busy} onClick={run}>
        {action.icon && createElement(moduleIcon(action.icon), { size: 15 })}
        {action.label}
      </Button>
      {error && <span className="text-[11px] text-danger">{error}</span>}
    </>
  );
}

function EmailReaderView({ payload }: { payload: unknown }) {
  const mail = EmailMessage.parse(payload);
  return (
    <article>
      <h3 className="font-serif text-lg text-ink">{mail.subject}</h3>
      <div className="mt-1 flex flex-wrap items-center gap-x-1.5 text-xs text-ink-faint">
        {mail.from && <span>{mail.from}</span>}
        {mail.from && mail.date && <span>·</span>}
        {mail.date && <span>{mail.date}</span>}
        {mail.unread && (
          <span className="rounded-full bg-accent-dim px-1.5 py-0.5 text-[11px] text-accent-strong">
            Unread
          </span>
        )}
      </div>
      <p className="mt-4 text-[15px] leading-relaxed whitespace-pre-wrap text-ink">{mail.body}</p>
      {mail.message_id && mail.actions.length > 0 && (
        <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-edge pt-4">
          {mail.actions.map((action) => (
            <MailActionButton
              key={action.tool}
              module={mail.module}
              messageId={mail.message_id}
              action={action}
            />
          ))}
        </div>
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
