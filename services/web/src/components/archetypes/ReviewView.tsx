/**
 * The `review` archetype (ADR-0033, #220): a queue of agent-proposed changes the operator
 * approves or rejects. Core-rendered — the module supplies only data. Each row opens the
 * shared review overlay (#KB-refactor), which shapes itself to the operation — a per-hunk
 * diff for an edit, a confirmation for a move/delete/folder/knowledge-base — with
 * Approve / Reject / Ignore. Nothing the agent proposes lands without the operator's
 * approval, so this is the gate on agent-initiated writes; the same overlay backs the chat
 * composer's suggestion bubble.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Inbox } from "lucide-react";
import { useState } from "react";

import { SuggestionReviewModal } from "@/components/SuggestionReviewModal";
import { Badge, Button, EmptyState, Spinner } from "@/components/ui";
import { api } from "@/lib/api";
import { type PendingSuggestion, ReviewData, type ReviewSuggestion } from "@/lib/contracts";

/** Badge tone per operation — additive green, removal danger, structural/edit accent. */
function operationTone(op: ReviewSuggestion["operation"]): "ok" | "accent" | "danger" {
  if (op === "create" || op === "mkdir" || op === "mkproject") return "ok";
  if (op === "delete") return "danger";
  return "accent";
}

function summary(s: ReviewSuggestion): string {
  return s.operation === "move" ? `${s.path} → ${s.to_path}` : s.path;
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

export function ReviewView({ module, pageId }: { module: string; pageId: string }) {
  const qc = useQueryClient();
  const [reviewing, setReviewing] = useState<PendingSuggestion | null>(null);
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
  const invalidate = () => void qc.invalidateQueries({ queryKey: ["module-page", module, pageId] });

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
          <li
            key={s.id}
            className="flex flex-col gap-3 rounded-(--radius-card) border border-edge bg-surface p-4"
          >
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={operationTone(s.operation)} className="uppercase">
                {s.operation}
              </Badge>
              <FileText size={14} className="shrink-0 text-ink-faint" />
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink" title={summary(s)}>
                {summary(s)}
              </span>
              <span className="shrink-0 text-xs text-ink-faint">
                {s.origin} · {formatWhen(s.created_at)}
              </span>
            </div>
            {s.note && <p className="text-sm text-ink-dim">{s.note}</p>}
            <div className="flex items-center justify-end">
              <Button
                variant="primary"
                onClick={() => setReviewing({ ...s, module, page_id: pageId })}
              >
                Review
              </Button>
            </div>
          </li>
        ))}
      </ul>
      {reviewing && (
        <SuggestionReviewModal
          key={reviewing.id}
          suggestion={reviewing}
          onClose={() => setReviewing(null)}
          onResolved={invalidate}
        />
      )}
    </div>
  );
}
