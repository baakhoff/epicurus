/**
 * The suggestion review overlay (#KB-refactor, ADR-0090). Every agent change to the
 * knowledge base is staged for review (ADR-0033); this core-owned window opens over the UI
 * — from the chat composer's suggestion bubble or the Suggestions page — and shows what's
 * needed, shaped by the operation:
 *   - update / create / append → a diff with per-hunk checkboxes, plus an editable draft
 *     the operator can hand-edit before approving ("edit anywhere before approving
 *     anything" — ADR-0090);
 *   - delete          → a confirmation showing what will be removed;
 *   - move            → a from → to confirmation;
 *   - mkdir / mkproject → a simple "create this?" confirmation.
 * Three actions: Approve (apply the current draft — ticked hunks, free edits, or both),
 * Reject (discard), Ignore (close; it stays pending on the Suggestions page).
 */
import { useMutation } from "@tanstack/react-query";
import { Check, FilePlus, FolderPlus, Library, Pencil, Trash2, X } from "lucide-react";
import { type ComponentType, useMemo, useState } from "react";

import { Markdown } from "@/components/Markdown";
import { Badge, Button, TextArea, cn } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import type { PendingSuggestion } from "@/lib/contracts";
import { type DiffLine, diffLines, mergeHunks, toHunks } from "@/lib/linediff";
import { toast } from "@/stores/toasts";

type Op = PendingSuggestion["operation"];

const OP_META: Record<Op, { label: string; icon: ComponentType<{ size?: number }>; tone: "ok" | "accent" | "danger" | "dim" }> = {
  create: { label: "New document", icon: FilePlus, tone: "ok" },
  update: { label: "Edit", icon: Pencil, tone: "accent" },
  append: { label: "Append", icon: Pencil, tone: "accent" },
  delete: { label: "Delete", icon: Trash2, tone: "danger" },
  move: { label: "Move", icon: Pencil, tone: "accent" },
  mkdir: { label: "New folder", icon: FolderPlus, tone: "ok" },
  mkproject: { label: "New knowledge base", icon: Library, tone: "ok" },
};

interface Segment {
  kind: "context" | "hunk";
  id?: number;
  lines: DiffLine[];
}

/** Split a diff into alternating context spans and reviewable change hunks. */
function toSegments(diff: DiffLine[]): Segment[] {
  const segs: Segment[] = [];
  let i = 0;
  let id = 0;
  while (i < diff.length) {
    if (diff[i].tag === "same") {
      const lines: DiffLine[] = [];
      while (i < diff.length && diff[i].tag === "same") lines.push(diff[i++]);
      segs.push({ kind: "context", lines });
    } else {
      const lines: DiffLine[] = [];
      while (i < diff.length && diff[i].tag !== "same") lines.push(diff[i++]);
      segs.push({ kind: "hunk", id: id++, lines });
    }
  }
  return segs;
}

function lineClass(tag: DiffLine["tag"]): string {
  if (tag === "add") return "bg-ok/10 text-ok";
  if (tag === "del") return "bg-danger/10 text-danger";
  return "text-ink-dim";
}

function linePrefix(tag: DiffLine["tag"]): string {
  return tag === "add" ? "+" : tag === "del" ? "-" : " ";
}

function DiffReview({
  diff,
  accepted,
  onToggle,
}: {
  diff: DiffLine[];
  accepted: Set<number>;
  onToggle: (id: number) => void;
}) {
  const segments = useMemo(() => toSegments(diff), [diff]);
  if (diff.length === 0) {
    return <p className="px-1 py-3 text-sm text-ink-dim">No textual changes.</p>;
  }
  return (
    <div className="overflow-hidden rounded-(--radius-field) border border-edge font-mono text-[12px] leading-relaxed">
      {segments.map((seg, s) =>
        seg.kind === "context" ? (
          <div key={`c${s}`}>
            {seg.lines.map((l, k) => (
              <div key={k} className={cn("px-3", lineClass(l.tag))}>
                <span className="select-none text-ink-faint">{linePrefix(l.tag)} </span>
                {l.text || " "}
              </div>
            ))}
          </div>
        ) : (
          <div key={`h${seg.id}`} className="border-y border-edge first:border-t-0 last:border-b-0">
            <label className="flex cursor-pointer items-center gap-2 bg-surface-2 px-3 py-1 text-[11px] text-ink-dim">
              {/* eslint-disable-next-line no-restricted-syntax -- native checkbox, not a text field/select */}
              <input
                type="checkbox"
                checked={accepted.has(seg.id!)}
                onChange={() => onToggle(seg.id!)}
                aria-label={`Apply change ${seg.id! + 1}`}
              />
              Apply this change
            </label>
            {seg.lines.map((l, k) => (
              <div
                key={k}
                className={cn("px-3", lineClass(l.tag), !accepted.has(seg.id!) && "opacity-40")}
              >
                <span className="select-none text-ink-faint">{linePrefix(l.tag)} </span>
                {l.text || " "}
              </div>
            ))}
          </div>
        ),
      )}
    </div>
  );
}

export function SuggestionReviewModal({
  suggestion,
  onClose,
  onResolved,
}: {
  suggestion: PendingSuggestion;
  onClose: () => void;
  onResolved?: () => void;
}) {
  const { module, page_id: pageId, id, operation, current, content } = suggestion;
  const meta = OP_META[operation];
  // update/create/append all show a diff with per-hunk approval (append's diff is the
  // added text); move/mkdir/mkproject/delete are confirmations.
  const isEdit = operation === "update" || operation === "create" || operation === "append";

  const diff = useMemo(
    () => (isEdit ? diffLines(current, content) : []),
    [isEdit, current, content],
  );
  // Every hunk starts accepted: the default is "approve the whole proposal".
  const [accepted, setAccepted] = useState<Set<number>>(
    () => new Set(toHunks(isEdit ? diffLines(current, content) : []).map((h) => h.id)),
  );
  const toggle = (hid: number) =>
    setAccepted((prev) => {
      const next = new Set(prev);
      if (next.has(hid)) next.delete(hid);
      else next.add(hid);
      return next;
    });

  // The editable draft (ADR-0090): defaults to the hunk-merged result and stays synced to
  // it until the operator types — from then on their free edit wins over further hunk
  // toggling, since re-deriving from hunks would silently discard what they typed. Adjusted
  // during render (not an effect) per the React-recommended pattern for state that tracks a
  // changing value until overridden: https://react.dev/reference/react/useState#storing-information-from-previous-renders
  const hunkMerged = useMemo(() => mergeHunks(diff, accepted), [diff, accepted]);
  const [draft, setDraft] = useState(hunkMerged);
  const [syncedMerge, setSyncedMerge] = useState(hunkMerged);
  const [edited, setEdited] = useState(false);
  if (!edited && hunkMerged !== syncedMerge) {
    setSyncedMerge(hunkMerged);
    setDraft(hunkMerged);
  }

  const resolved = () => {
    onResolved?.();
    onClose();
  };
  const onError = (err: unknown) =>
    toast.error(err instanceof ApiError ? err.detail : "Action failed.");

  const approve = useMutation({
    mutationFn: () => api.approveSuggestion(module, pageId, id, isEdit ? draft : undefined),
    onSuccess: resolved,
    onError,
  });
  const reject = useMutation({
    mutationFn: () => api.rejectSuggestion(module, pageId, id),
    onSuccess: resolved,
    onError,
  });
  const busy = approve.isPending || reject.isPending;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Review suggestion"
    >
      <button
        type="button"
        aria-hidden
        tabIndex={-1}
        className="absolute inset-0 cursor-default"
        onClick={onClose}
      />
      <div className="relative flex max-h-[85vh] w-full max-w-3xl flex-col rounded-(--radius-card) border border-edge bg-surface shadow-(--ep-shadow)">
        {/* header */}
        <header className="flex items-center gap-2 border-b border-edge px-4 py-3">
          <Badge tone={meta.tone} className="uppercase">
            {meta.label}
          </Badge>
          <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink" title={suggestion.path}>
            {suggestion.path}
          </span>
          <button
            onClick={onClose}
            aria-label="Close"
            className="rounded-md p-1 text-ink-dim hover:bg-surface-2 hover:text-ink"
          >
            <X size={16} />
          </button>
        </header>

        {/* body */}
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {suggestion.note && (
            <p className="mb-3 rounded-(--radius-field) bg-surface-2 px-3 py-2 text-sm text-ink-dim">
              {suggestion.note}
            </p>
          )}

          {operation === "move" && (
            <p className="text-sm text-ink">
              Move <span className="font-mono text-xs">{suggestion.path}</span> to{" "}
              <span className="font-mono text-xs text-accent-strong">{suggestion.to_path}</span>?
            </p>
          )}
          {operation === "mkdir" && (
            <p className="text-sm text-ink">
              Create the folder <span className="font-mono text-xs">{suggestion.path}</span>?
            </p>
          )}
          {operation === "mkproject" && (
            <p className="text-sm text-ink">
              Create the knowledge base{" "}
              <span className="font-mono text-xs">{suggestion.path}</span>?
            </p>
          )}
          {operation === "delete" && (
            <div>
              <p className="mb-2 text-sm text-danger">
                This will permanently delete the document. Its current content:
              </p>
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-(--radius-field) border border-edge bg-surface-2 p-3 font-mono text-[12px] text-ink-dim">
                {suggestion.current || "(empty)"}
              </pre>
            </div>
          )}
          {isEdit && (
            <>
              <p className="mb-2 text-xs text-ink-faint">
                Tick the changes to apply — unticked changes are left as they are.
              </p>
              <DiffReview diff={diff} accepted={accepted} onToggle={toggle} />
              <div className="mt-3">
                <p className="mb-1 text-xs text-ink-faint">
                  Edit the draft directly before approving — your edits take over from the
                  ticked changes above.
                </p>
                <TextArea
                  value={draft}
                  onChange={(e) => {
                    setDraft(e.target.value);
                    setEdited(true);
                  }}
                  rows={10}
                  className="font-mono text-[12px]"
                  aria-label="Editable draft"
                />
              </div>
              {operation === "create" && draft && (
                <details className="mt-3">
                  <summary className="cursor-pointer text-xs text-ink-dim">Preview rendered</summary>
                  <div className="mt-2 rounded-(--radius-field) border border-edge p-3">
                    <Markdown>{draft}</Markdown>
                  </div>
                </details>
              )}
            </>
          )}
        </div>

        {/* footer */}
        <footer className="flex items-center justify-end gap-2 border-t border-edge px-4 py-3">
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Ignore
          </Button>
          <Button variant="outline" onClick={() => reject.mutate()} disabled={busy} busy={reject.isPending}>
            <X size={15} /> Reject
          </Button>
          <Button variant="primary" onClick={() => approve.mutate()} disabled={busy} busy={approve.isPending}>
            <Check size={15} /> Approve
          </Button>
        </footer>
      </div>
    </div>
  );
}
