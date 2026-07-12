/**
 * The `review` archetype (ADR-0033, #220): a queue of agent-proposed changes the operator
 * approves or rejects. Core-rendered — the module supplies only data. Each row opens the
 * shared review overlay (#KB-refactor, ADR-0090), which shapes itself to the operation — a
 * per-hunk diff plus an editable draft for an edit, a confirmation for a
 * move/delete/folder/knowledge-base — with Approve / Reject / Ignore. Nothing the agent
 * proposes lands without the operator's approval, so this is the gate on agent-initiated
 * writes; the same overlay backs the chat composer's suggestion bubble. A "Recently
 * resolved" panel below the queue surfaces the audit trail (ADR-0090): what was proposed
 * vs. what was actually approved, including any edit.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Inbox } from "lucide-react";
import { useMemo, useState } from "react";

import { SuggestionReviewModal } from "@/components/SuggestionReviewModal";
import { Badge, Button, EmptyState, Spinner, Switch, cn } from "@/components/ui";
import { api } from "@/lib/api";
import { type PendingSuggestion, type ReviewDecision, ReviewData } from "@/lib/contracts";
import { type DiffLine, diffLines } from "@/lib/linediff";
import { formatWhen, operationTone, suggestionTarget } from "@/lib/suggestions";

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
                    title={suggestionTarget(s)}
                  >
                    {suggestionTarget(s)}
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
        <ResolvedHistory module={module} pageId={pageId} />
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

/** A read-only line diff for the audit trail — no hunk toggles, just what changed. */
function AuditDiffLines({ before, after }: { before: string; after: string }) {
  const diff = useMemo(() => diffLines(before, after), [before, after]);
  if (diff.length === 0) {
    return <p className="px-1 py-2 text-xs text-ink-dim">No textual difference.</p>;
  }
  const lineClass = (tag: DiffLine["tag"]) =>
    tag === "add" ? "bg-ok/10 text-ok" : tag === "del" ? "bg-danger/10 text-danger" : "text-ink-dim";
  const linePrefix = (tag: DiffLine["tag"]) => (tag === "add" ? "+" : tag === "del" ? "-" : " ");
  return (
    <div className="max-h-64 overflow-auto rounded-(--radius-field) border border-edge font-mono text-[12px] leading-relaxed">
      {diff.map((l, i) => (
        <div key={i} className={cn("px-3", lineClass(l.tag))}>
          <span className="select-none text-ink-faint">{linePrefix(l.tag)} </span>
          {l.text || " "}
        </div>
      ))}
    </div>
  );
}

/** One resolved suggestion in the "Recently resolved" audit trail (ADR-0090). */
function DecisionRow({ decision }: { decision: ReviewDecision }) {
  const [open, setOpen] = useState(false);
  const hasContent = decision.proposed_content || decision.applied_content;
  return (
    <li className="flex flex-col gap-2 rounded-(--radius-card) border border-edge bg-surface p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={operationTone(decision.operation)} className="uppercase">
          {decision.operation}
        </Badge>
        <Badge tone={decision.decision === "approved" ? "ok" : "danger"} className="uppercase">
          {decision.decision}
        </Badge>
        <span
          className="min-w-0 flex-1 truncate font-mono text-xs text-ink"
          title={decision.to_path || decision.path}
        >
          {decision.to_path || decision.path}
        </span>
        <span className="shrink-0 text-xs text-ink-faint">
          {decision.origin} · {formatWhen(decision.decided_at)}
        </span>
      </div>
      {decision.note && <p className="text-xs text-ink-dim">{decision.note}</p>}
      {hasContent && (
        <>
          <button
            type="button"
            className="self-start text-xs text-accent-strong hover:underline"
            onClick={() => setOpen((v) => !v)}
          >
            {open ? "Hide what changed" : "See what changed"}
          </button>
          {open &&
            (decision.decision === "approved" ? (
              <AuditDiffLines before={decision.proposed_content} after={decision.applied_content} />
            ) : (
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-(--radius-field) border border-edge bg-surface-2 p-3 font-mono text-[12px] text-ink-dim">
                {decision.proposed_content || "(no content)"}
              </pre>
            ))}
        </>
      )}
    </li>
  );
}

/** The resolved-decision audit trail (ADR-0090): what was proposed vs. what was applied. */
function ResolvedHistory({ module, pageId }: { module: string; pageId: string }) {
  const query = useQuery({
    queryKey: ["review-audit", module, pageId],
    queryFn: () => api.reviewAudit(module, pageId),
  });
  const decisions = query.data?.decisions ?? [];
  if (decisions.length === 0) return null;
  return (
    <details className="mx-auto max-w-3xl px-4 pb-4">
      <summary className="cursor-pointer text-xs text-ink-dim">
        Recently resolved ({decisions.length})
      </summary>
      <ul className="mt-3 flex flex-col gap-2">
        {decisions.map((d) => (
          <DecisionRow key={`${d.id}-${d.decided_at}`} decision={d} />
        ))}
      </ul>
    </details>
  );
}
