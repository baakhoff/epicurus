/**
 * Suggestions — the one place for agent-proposed changes across every module (#KB-refactor).
 *
 * The agent never writes directly: each module that opts into review (knowledge today, notes
 * and others as they adopt the `review` archetype) stages its proposals, and the core serves
 * them as one cross-module feed (`GET /platform/v1/suggestions`). This surface groups that feed
 * by module — each group carries the module's review on/off toggle and its pending changes —
 * so the operator approves or rejects from a single inbox rather than hunting per-module pages.
 * The same `SuggestionReviewModal` (and the `["suggestions"]` query) back the chat composer's
 * suggestion bubble, so acting here updates there and vice-versa.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Inbox } from "lucide-react";
import { createElement, useState } from "react";

import { reviewPageNavs } from "@/app/registry";
import { SuggestionReviewModal } from "@/components/SuggestionReviewModal";
import { Badge, Button, EmptyState, Spinner, Switch } from "@/components/ui";
import { api } from "@/lib/api";
import type { PendingSuggestion } from "@/lib/contracts";
import { moduleIcon } from "@/lib/icons";
import { formatWhen, moduleLabel, operationTone, suggestionTarget } from "@/lib/suggestions";

function SuggestionRow({
  suggestion: s,
  onReview,
}: {
  suggestion: PendingSuggestion;
  onReview: (s: PendingSuggestion) => void;
}) {
  return (
    <li className="flex flex-col gap-3 rounded-(--radius-card) border border-edge bg-surface p-4">
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
        <Button variant="primary" onClick={() => onReview(s)}>
          Review
        </Button>
      </div>
    </li>
  );
}

/** One module's section of the inbox: its review toggle + its pending suggestions. */
function ModuleGroup({
  module,
  icon,
  items,
  onReview,
}: {
  module: string;
  icon: string | undefined;
  items: PendingSuggestion[];
  onReview: (s: PendingSuggestion) => void;
}) {
  const qc = useQueryClient();
  const enabledQuery = useQuery({
    queryKey: ["suggestions-enabled", module],
    queryFn: () => api.suggestionsEnabled(module),
  });
  const toggle = useMutation({
    mutationFn: (next: boolean) => api.setSuggestionsEnabled(module, next),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["suggestions-enabled", module] }),
  });
  const reviewOn = enabledQuery.data?.enabled ?? true;

  return (
    <section>
      <div className="flex items-center justify-between gap-3 border-b border-edge pb-2">
        <div className="flex min-w-0 items-center gap-2">
          {createElement(moduleIcon(icon), { size: 16, className: "shrink-0 text-ink-faint" })}
          <h2 className="truncate font-serif text-base text-ink">{moduleLabel(module)}</h2>
          {items.length > 0 && <Badge tone="accent">{items.length}</Badge>}
        </div>
        <Switch
          checked={reviewOn}
          onChange={(next) => toggle.mutate(next)}
          disabled={toggle.isPending || enabledQuery.isLoading}
          label={`Review ${moduleLabel(module)} changes before applying`}
        />
      </div>
      {items.length === 0 ? (
        <p className="px-1 py-3 text-sm text-ink-faint">
          {reviewOn
            ? "Nothing pending."
            : "Review off — the agent's changes apply automatically, without review."}
        </p>
      ) : (
        <ul className="flex flex-col gap-3 pt-3">
          {items.map((s) => (
            <SuggestionRow key={s.id} suggestion={s} onReview={onReview} />
          ))}
        </ul>
      )}
    </section>
  );
}

export function SuggestionsScreen() {
  const qc = useQueryClient();
  const modules = useQuery({ queryKey: ["modules"], queryFn: () => api.modules(), staleTime: 30_000 });
  const feed = useQuery({ queryKey: ["suggestions"], queryFn: api.suggestions });
  const [reviewing, setReviewing] = useState<PendingSuggestion | null>(null);

  const reviewModules = reviewPageNavs(modules.data ?? []);
  const iconFor = (name: string) =>
    modules.data?.find((m) => m.manifest.name === name)?.manifest.ui?.icon;
  const items = feed.data ?? [];

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b border-edge px-4 py-2.5">
        <Inbox size={16} className="shrink-0 text-ink-faint" />
        <h1 className="font-serif text-base text-ink">Suggestions</h1>
        <span className="text-xs text-ink-faint">agent-proposed changes</span>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {modules.isLoading || feed.isLoading ? (
          <div className="flex h-full items-center justify-center">
            <Spinner />
          </div>
        ) : reviewModules.length === 0 ? (
          <div className="flex h-full items-center justify-center p-6">
            <EmptyState quote="Nothing proposes changes yet.">
              <p className="flex items-center gap-2 text-sm text-ink-dim">
                <Inbox size={15} />
                When a module lets the agent suggest edits, they gather here for your approval.
              </p>
            </EmptyState>
          </div>
        ) : (
          <div className="mx-auto flex max-w-3xl flex-col gap-6 p-4">
            {reviewModules.map((rm) => (
              <ModuleGroup
                key={rm.module}
                module={rm.module}
                icon={iconFor(rm.module)}
                items={items.filter((s) => s.module === rm.module)}
                onReview={setReviewing}
              />
            ))}
          </div>
        )}
      </div>

      {reviewing && (
        <SuggestionReviewModal
          key={reviewing.id}
          suggestion={reviewing}
          onClose={() => setReviewing(null)}
          onResolved={() => void qc.invalidateQueries({ queryKey: ["suggestions"] })}
        />
      )}
    </div>
  );
}
