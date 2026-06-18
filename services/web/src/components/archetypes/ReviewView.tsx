/**
 * The `review` archetype (ADR-0033, #220): a queue of agent-proposed changes the
 * operator approves or rejects. Core-rendered — the module supplies only data: a list
 * of suggestions, each with a server-computed unified `diff`. Approve applies the change
 * and re-indexes it; reject discards it. Nothing the agent proposes lands without the
 * operator's approval, so this is the gate on agent-initiated writes.
 *
 * Knowledge is the first user. Approve/reject are deliberately *not* module tools (the
 * agent could otherwise approve its own proposals); they are operator-only endpoints the
 * shell calls through the core proxy.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, FileText, Inbox, X } from "lucide-react";

import { Badge, Button, EmptyState, Spinner, cn } from "@/components/ui";
import { api } from "@/lib/api";
import { ReviewData, type ReviewSuggestion } from "@/lib/contracts";

/** Badge tone for each operation — additive green, edit accent, removal danger. */
function operationTone(op: ReviewSuggestion["operation"]): "ok" | "accent" | "danger" {
  return op === "create" ? "ok" : op === "delete" ? "danger" : "accent";
}

/** Colour one unified-diff line by its prefix (hunk header, addition, removal, context). */
function diffLineClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---")) return "text-ink-faint";
  if (line.startsWith("@@")) return "text-accent";
  if (line.startsWith("+")) return "bg-ok/10 text-ok";
  if (line.startsWith("-")) return "bg-danger/10 text-danger";
  return "text-ink-dim";
}

function DiffBlock({ diff }: { diff: string }) {
  if (!diff.trim()) {
    return <p className="px-1 py-2 text-xs text-ink-faint">No textual changes.</p>;
  }
  // Drop a trailing empty line so the block doesn't end with a blank row.
  const lines = diff.replace(/\n$/, "").split("\n");
  return (
    <pre className="max-h-80 overflow-auto rounded-(--radius-field) border border-edge bg-surface-2 font-mono text-[11px] leading-relaxed">
      {lines.map((line, i) => (
        <div key={i} className={cn("px-3", diffLineClass(line))}>
          {line || " "}
        </div>
      ))}
    </pre>
  );
}

function formatWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function SuggestionCard({
  module,
  pageId,
  suggestion,
}: {
  module: string;
  pageId: string;
  suggestion: ReviewSuggestion;
}) {
  const qc = useQueryClient();
  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ["module-page", module, pageId] });

  const approve = useMutation({
    mutationFn: () => api.approveSuggestion(module, pageId, suggestion.id),
    onSuccess: invalidate,
  });
  const reject = useMutation({
    mutationFn: () => api.rejectSuggestion(module, pageId, suggestion.id),
    onSuccess: invalidate,
  });

  const busy = approve.isPending || reject.isPending;
  const error = (approve.error || reject.error) as Error | null;

  return (
    <li className="flex flex-col gap-3 rounded-(--radius-card) border border-edge bg-surface p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={operationTone(suggestion.operation)} className="uppercase">
          {suggestion.operation}
        </Badge>
        <FileText size={14} className="shrink-0 text-ink-faint" />
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink" title={suggestion.path}>
          {suggestion.path}
        </span>
        <span className="shrink-0 text-xs text-ink-faint">
          {suggestion.origin} · {formatWhen(suggestion.created_at)}
        </span>
      </div>

      {suggestion.note && <p className="text-sm text-ink-dim">{suggestion.note}</p>}

      <DiffBlock diff={suggestion.diff} />

      {error && <p className="text-xs text-danger">{error.message}</p>}

      <div className="flex items-center justify-end gap-2">
        <Button
          variant="ghost"
          onClick={() => {
            if (
              window.confirm(
                `Discard the proposed ${suggestion.operation} of "${suggestion.path}"?`,
              )
            ) {
              reject.mutate();
            }
          }}
          disabled={busy}
          busy={reject.isPending}
        >
          <X size={15} /> Reject
        </Button>
        <Button
          variant="primary"
          onClick={() => approve.mutate()}
          disabled={busy}
          busy={approve.isPending}
        >
          <Check size={15} /> Approve
        </Button>
      </div>
    </li>
  );
}

export function ReviewView({ module, pageId }: { module: string; pageId: string }) {
  const query = useQuery({
    queryKey: ["module-page", module, pageId],
    queryFn: () => api.modulePage(module, pageId),
  });

  if (query.isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <EmptyState quote="This page is resting.">
          <p className="text-sm text-ink-dim">{(query.error as Error).message}</p>
        </EmptyState>
      </div>
    );
  }

  const data = ReviewData.parse(query.data ?? {});

  if (data.suggestions.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <EmptyState quote="Nothing awaits review.">
          <p className="flex items-center gap-2 text-sm text-ink-dim">
            <Inbox size={15} />
            Changes the agent proposes appear here for your approval.
          </p>
        </EmptyState>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <ul className="mx-auto flex max-w-3xl flex-col gap-3 p-4">
        {data.suggestions.map((s) => (
          <SuggestionCard key={s.id} module={module} pageId={pageId} suggestion={s} />
        ))}
      </ul>
    </div>
  );
}
