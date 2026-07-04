/**
 * Chat composer attach affordance (ADR-0019). Lets the user attach context to a turn:
 * an uploaded **file**, another **chat**, or an entity from an **enabled, attachable
 * module**. Selections become pills above the composer; the agent expands them at turn
 * time. The shell renders everything — modules only supply pickable data.
 */
import { useMutation, useQuery } from "@tanstack/react-query";
import { Blocks, ChevronLeft, ChevronRight, File, MessageSquare, Paperclip, X } from "lucide-react";
import { useRef, useState } from "react";

import { Sheet, Spinner, cn } from "@/components/ui";
import { api } from "@/lib/api";
import type { Attachment } from "@/lib/contracts";
import { moduleIcon } from "@/lib/icons";

function uid(): string {
  return crypto.randomUUID();
}

const SOURCE_ICON = { file: File, chat: MessageSquare, module: Blocks } as const;

/** A pill for an attached item, with a remove affordance. */
export function AttachmentPill({
  attachment,
  onRemove,
}: {
  attachment: Attachment;
  onRemove?: () => void;
}) {
  const Icon = SOURCE_ICON[attachment.source] ?? File;
  return (
    <span className="inline-flex max-w-48 items-center gap-1 rounded-full border border-edge bg-surface-2 px-2 py-0.5 text-xs text-ink-dim">
      <Icon size={11} className="shrink-0" />
      <span className="truncate">{attachment.title || attachment.source}</span>
      {onRemove && (
        <button onClick={onRemove} aria-label={`Remove ${attachment.title || "attachment"}`}>
          <X size={12} className="text-ink-faint hover:text-ink" />
        </button>
      )}
    </span>
  );
}

/** The in-flight placeholder (#489): the same pill silhouette with a spinner while a
 *  paste/drop upload runs; it swaps for a real AttachmentPill (or an error toast) when
 *  the server answers. */
export function PendingAttachmentPill({ name }: { name: string }) {
  return (
    <span className="inline-flex max-w-48 items-center gap-1 rounded-full border border-edge bg-surface-2 px-2 py-0.5 text-xs text-ink-dim">
      <Spinner className="size-3 shrink-0" />
      <span className="truncate">{name}</span>
    </span>
  );
}

function SectionTitle({ children }: { children: string }) {
  return (
    <h4 className="mb-2 text-xs font-medium tracking-wide text-ink-faint uppercase">{children}</h4>
  );
}

const rowClass =
  "flex w-full items-center gap-2 rounded-(--radius-field) px-3 py-2 text-left text-sm text-ink hover:bg-surface-2";

function FileSection({ onAttach }: { onAttach: (a: Attachment) => void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const upload = useMutation({
    mutationFn: (file: File) => api.uploadAttachment(file),
    onSuccess: (res) =>
      onAttach({ att_id: res.att_id, source: "file", kind: res.kind, title: res.title }),
  });
  return (
    <section>
      <SectionTitle>File</SectionTitle>
      {/* eslint-disable-next-line no-restricted-syntax -- hidden native file picker, opened by the button below; not a styled field */}
      <input
        ref={inputRef}
        type="file"
        className="hidden"
        aria-label="Upload a file"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) upload.mutate(file);
          e.target.value = "";
        }}
      />
      <button className={rowClass} onClick={() => inputRef.current?.click()} disabled={upload.isPending}>
        {upload.isPending ? <Spinner className="size-4" /> : <File size={15} />}
        Upload a file
      </button>
      {upload.isError && (
        <p className="mt-1 px-3 text-xs text-danger">{(upload.error as Error).message}</p>
      )}
    </section>
  );
}

function ChatSection({ onAttach, enabled }: { onAttach: (a: Attachment) => void; enabled: boolean }) {
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: api.sessions, enabled });
  const items = sessions.data ?? [];
  return (
    <section>
      <SectionTitle>Another chat</SectionTitle>
      {items.length === 0 ? (
        <p className="px-3 text-xs text-ink-dim">No other conversations yet.</p>
      ) : (
        <div className="flex flex-col">
          {items.slice(0, 8).map((session) => (
            <button
              key={session.id}
              className={rowClass}
              onClick={() =>
                onAttach({
                  att_id: uid(),
                  source: "chat",
                  kind: "chat",
                  ref_id: session.id,
                  title: session.title || "untitled",
                })
              }
            >
              <MessageSquare size={15} className="shrink-0 text-ink-faint" />
              <span className="truncate">{session.title || "untitled"}</span>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

function ModuleSection({
  onAttach,
  enabled,
}: {
  onAttach: (a: Attachment) => void;
  enabled: boolean;
}) {
  const [moduleName, setModuleName] = useState<string | null>(null);
  const modules = useQuery({ queryKey: ["modules"], queryFn: () => api.modules(), enabled });
  const attachable = (modules.data ?? []).filter(
    (m) => m.status.healthy && m.enabled && m.manifest.attachable,
  );
  const items = useQuery({
    queryKey: ["module-attachments", moduleName],
    queryFn: () => api.moduleAttachments(moduleName as string),
    enabled: Boolean(moduleName),
  });

  return (
    <section>
      <SectionTitle>From a module</SectionTitle>
      {attachable.length === 0 ? (
        <p className="px-3 text-xs text-ink-dim">No attachable modules enabled.</p>
      ) : !moduleName ? (
        <div className="flex flex-col">
          {attachable.map((m) => {
            const Icon = moduleIcon(m.manifest.ui?.icon);
            return (
              <button key={m.manifest.name} className={rowClass} onClick={() => setModuleName(m.manifest.name)}>
                <Icon size={15} className="shrink-0 text-ink-faint" />
                <span className="flex-1 truncate">{m.manifest.name}</span>
                <ChevronRight size={14} className="text-ink-faint" />
              </button>
            );
          })}
        </div>
      ) : (
        <div className="flex flex-col">
          <button
            className="mb-1 inline-flex items-center gap-1 px-3 text-xs text-ink-dim hover:text-ink"
            onClick={() => setModuleName(null)}
          >
            <ChevronLeft size={13} /> {moduleName}
          </button>
          {items.isLoading && <Spinner className="mx-3 size-4" />}
          {items.data?.length === 0 && <p className="px-3 text-xs text-ink-dim">Nothing to attach.</p>}
          {items.data?.map((item) => (
            <button
              key={item.ref_id}
              className={rowClass}
              onClick={() =>
                onAttach({
                  att_id: uid(),
                  source: "module",
                  module: moduleName,
                  ref_id: item.ref_id,
                  kind: item.kind,
                  title: item.title || item.ref_id,
                })
              }
            >
              <span className="truncate">{item.title || item.ref_id}</span>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

export function AttachButton({ onAttach }: { onAttach: (a: Attachment) => void }) {
  const [open, setOpen] = useState(false);
  const attach = (a: Attachment) => {
    onAttach(a);
    setOpen(false);
  };
  return (
    <>
      <button
        type="button"
        aria-label="Attach context"
        onClick={() => setOpen(true)}
        className={cn(
          "flex h-[42px] items-center rounded-(--radius-field) px-2 text-ink-dim",
          "transition-colors hover:bg-surface-2 hover:text-ink",
        )}
      >
        <Paperclip size={18} />
      </button>
      <Sheet open={open} onClose={() => setOpen(false)} title="Attach context">
        <div className="flex flex-col gap-5">
          <FileSection onAttach={attach} />
          <ChatSection onAttach={attach} enabled={open} />
          <ModuleSection onAttach={attach} enabled={open} />
        </div>
      </Sheet>
    </>
  );
}
