/**
 * The one message renderer (ADR-0087), shared by the right-panel `email-reader` (ADR-0024)
 * and the `mailbox` page's thread pane — the two never fork (#550). It renders a message's
 * subject (optional), sender/date/unread, its **plain-text** body (the module decodes HTML to
 * text server-side, so no HTML is ever rendered here — and JSX escapes the text anyway),
 * its attachments as core-proxied download links, and its tool-backed actions.
 *
 * The surfaces differ only in what an action does afterwards (`onActed`: the panel re-fetches
 * + swaps itself; the page invalidates the thread query) and whether the subject shows —
 * passed as props, so there is one component, not two.
 */
import { Paperclip } from "lucide-react";
import { createElement, useCallback, useState } from "react";

import { Button, Confirm } from "@/components/ui";
import { api } from "@/lib/api";
import type { BoardAction, EmailMessage, MailAttachment } from "@/lib/contracts";
import { moduleIcon } from "@/lib/icons";

/** A compact human size for an attachment (e.g. `24 KB`, `1.3 MB`). */
function formatSize(bytes: number): string {
  if (bytes <= 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

/**
 * One tool-backed action on a message (ADR-0024): mark read/unread, archive, trash. Invokes
 * the module's MCP tool through the core proxy, then calls `onActed` so the surface refreshes.
 * A `danger` action (Trash) is gated behind the shared Confirm dialog first.
 */
function MailActionButton({
  module,
  action,
  onActed,
}: {
  module: string;
  action: BoardAction;
  onActed: () => void | Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const run = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await api.invokeModuleTool(module, action.tool, action.args);
      await onActed();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Action failed");
    } finally {
      setBusy(false);
    }
  }, [module, action.tool, action.args, onActed]);

  const variant = action.intent === "danger" ? "danger" : "outline";
  return (
    <>
      <Button
        variant={variant}
        busy={busy}
        onClick={() => (action.confirm ? setConfirmOpen(true) : run())}
      >
        {action.icon && createElement(moduleIcon(action.icon), { size: 15 })}
        {action.label}
      </Button>
      {error && <span className="text-[11px] text-danger">{error}</span>}
      {action.confirm && (
        <Confirm
          open={confirmOpen}
          danger={action.intent === "danger"}
          message={action.confirm}
          confirmLabel={action.label}
          onCancel={() => setConfirmOpen(false)}
          onConfirm={() => {
            setConfirmOpen(false);
            void run();
          }}
        />
      )}
    </>
  );
}

/** One attachment as a same-origin download link through the core proxy (ADR-0087). */
function AttachmentLink({ href, attachment }: { href: string; attachment: MailAttachment }) {
  const size = formatSize(attachment.size);
  return (
    <a
      href={href}
      download={attachment.filename}
      className="inline-flex max-w-full items-center gap-1.5 rounded-(--radius-field) border border-edge px-2 py-1 text-xs text-ink-dim transition-colors hover:border-accent hover:text-accent-strong"
    >
      <Paperclip size={13} className="shrink-0" />
      <span className="truncate">{attachment.filename}</span>
      {size && <span className="shrink-0 text-ink-faint">{size}</span>}
    </a>
  );
}

export function MailMessageView({
  message,
  showSubject = true,
  attachmentUrl,
  onActed,
}: {
  message: EmailMessage;
  /** Show the message's own subject heading — true in the panel, false per-message in a
   *  thread (the thread's subject is shown once above the conversation). */
  showSubject?: boolean;
  /** Builds a same-origin download URL for one of this message's attachments; when omitted
   *  attachments aren't shown (the panel reader doesn't fetch them). */
  attachmentUrl?: (messageId: string, attachmentId: string) => string;
  /** Called after a successful action so the surface refreshes (panel swap / thread refetch). */
  onActed?: () => void | Promise<void>;
}) {
  return (
    <article>
      {showSubject && <h3 className="font-serif text-lg text-ink">{message.subject}</h3>}
      <div className="mt-1 flex flex-wrap items-center gap-x-1.5 text-xs text-ink-faint">
        {message.from && <span>{message.from}</span>}
        {message.from && message.date && <span>·</span>}
        {message.date && <span>{message.date}</span>}
        {message.unread && (
          <span className="rounded-full bg-accent-dim px-1.5 py-0.5 text-[11px] text-accent-strong">
            Unread
          </span>
        )}
      </div>
      <p className="mt-4 text-[15px] leading-relaxed whitespace-pre-wrap text-ink">
        {message.body}
      </p>
      {attachmentUrl && message.attachments.length > 0 && (
        <div className="mt-4 flex flex-wrap gap-2 border-t border-edge pt-3">
          {message.attachments.map((att) => (
            <AttachmentLink
              key={att.id}
              href={attachmentUrl(message.message_id, att.id)}
              attachment={att}
            />
          ))}
        </div>
      )}
      {onActed && message.message_id && message.actions.length > 0 && (
        <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-edge pt-4">
          {message.actions.map((action) => (
            <MailActionButton
              key={action.tool}
              module={message.module}
              action={action}
              onActed={onActed}
            />
          ))}
        </div>
      )}
    </article>
  );
}
