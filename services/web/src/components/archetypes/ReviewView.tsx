/**
 * The `review` archetype (ADR-0033, #220): a queue of agent-proposed changes the operator
 * approves or rejects. Core-rendered — the module supplies only data. Each row opens the
 * shared review overlay (#KB-refactor), which shapes itself to the operation — a per-hunk
 * diff for an edit, a confirmation for a move/delete/folder/knowledge-base — with
 * Approve / Reject / Ignore. Nothing the agent proposes lands without the operator's
 * approval, so this is the gate on agent-initiated writes; the same overlay backs the chat
 * composer's suggestion bubble.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Inbox } from "lucide-react";
import { useState } from "react";

import { SuggestionReviewModal } from "@/components/SuggestionReviewModal";
import { Badge, Button, EmptyState, Spinner, Switch } from "@/components/ui";
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
  // The per-module review on/off toggle (#KB-refactor): off ⇒ the agent's changes apply
  // directly instead of being staged here.
  const enabledQuery = useQuery({
    queryKey: ["suggestions-enabled", module],
    queryFn: () => api.suggestionsEnabled(module),
  });
  const toggle = useMutation({
    mutationFn: (next: boolean) => api.setSuggestionsEnabled(module, next),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["suggestions-enabled", module] }),
  });
  const reviewOn = enabledQuery.data?.enabled ?? true;

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

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* the per-module review on/off toggle — always shown (#KB-refactor) */}
      <div className="flex items-center justify-between gap-3 border-b border-edge px-4 py-2.5">
        <div className="min-w-0">
          <p className="text-sm text-ink">Review agent changes before applying</p>
          <p className="text-xs text-ink-faint">
            {reviewOn
              ? "Changes the agent proposes are staged here for your approval."
              : "Off — the agent's changes are applied automatically, without review."}
          </p>
        </div>
        <Switch
          checked={reviewOn}
          onChange={(next) => toggle.mutate(next)}
          disabled={toggle.isPending || enabledQuery.isLoading}
          label="Review agent changes before applying"
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {data.suggestions.length === 0 ? (
          <div className="flex h-full items-center justify-center p-6">
            <EmptyState quote={reviewOn ? "Nothing awaits review." : "Review is off."}>
              <p className="flex items-center gap-2 text-sm text-ink-dim">
                <Inbox size={15} />
                {reviewOn
                  ? "Changes the agent proposes appear here for your approval."
                  : "Turn review on to approve the agent's changes before they apply."}
              </p>
            </EmptyState>
          </div>
        ) : (
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
                  <span
                    className="min-w-0 flex-1 truncate font-mono text-xs text-ink"
                    title={summary(s)}
                  >
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
        )}
      </div>
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
