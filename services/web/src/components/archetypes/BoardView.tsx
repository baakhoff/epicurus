/**
 * The `board` archetype (ADR-0018): columns of cards, core-rendered. The module
 * supplies only data — columns, cards, and declarative *actions* — through the core
 * page proxy; this screen renders it in ε style. No module markup runs here.
 *
 * Unlike `browser`, a board mutates: each action names one of the module's MCP
 * tools, which the shell invokes through the core (`invokeModuleTool`, validated
 * against the manifest). A `form` action collects arguments via the shared
 * SchemaForm first; a `confirm` action gates a one-tap call behind a dialog. After
 * any successful call the page data is refetched, so the board reflects the change.
 *
 * Columns scroll horizontally (kanban-style) on every width.
 */
import { useQuery } from "@tanstack/react-query";

import { Badge, EmptyState, Spinner, cn } from "@/components/ui";
import { api } from "@/lib/api";
import { BoardData, type BoardCard } from "@/lib/contracts";

import { ActionControl } from "./ActionControl";

function BoardCardView({
  module,
  pageId,
  card,
}: {
  module: string;
  pageId: string;
  card: BoardCard;
}) {
  return (
    <div className="rounded-(--radius-card) border border-edge bg-surface p-3">
      <p className={cn("text-sm leading-snug text-ink", card.done && "text-ink-faint line-through")}>
        {card.title}
      </p>
      {card.subtitle && <p className="mt-0.5 truncate text-xs text-ink-faint">{card.subtitle}</p>}
      {card.badges.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {card.badges.map((badge, i) => (
            <Badge key={`${badge.label}-${i}`} tone={badge.tone}>
              {badge.label}
            </Badge>
          ))}
        </div>
      )}
      {card.actions.length > 0 && (
        <div className="mt-2.5 flex flex-wrap items-center gap-1 border-t border-edge pt-2">
          {card.actions.map((action) => (
            <ActionControl
              key={action.tool + action.label}
              module={module}
              pageId={pageId}
              action={action}
              compact
            />
          ))}
        </div>
      )}
    </div>
  );
}

export function BoardView({ module, pageId }: { module: string; pageId: string }) {
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

  const data = BoardData.parse(query.data ?? {});
  const hasCards = data.columns.some((column) => column.cards.length > 0);

  return (
    <div className="flex h-full min-h-0 flex-col">
      {data.actions.length > 0 && (
        <div className="flex items-center justify-end gap-2 px-4 py-2">
          {data.actions.map((action) => (
            <ActionControl
              key={action.tool + action.label}
              module={module}
              pageId={pageId}
              action={action}
            />
          ))}
        </div>
      )}

      {!hasCards ? (
        <div className="flex min-h-0 flex-1 items-center justify-center p-6">
          <EmptyState quote="Nothing on the board yet." />
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 gap-4 overflow-x-auto p-4">
          {data.columns.map((column) => (
            <section key={column.id} className="flex w-72 shrink-0 flex-col">
              <header className="mb-2 flex items-center gap-2 px-1">
                <h2 className="text-xs font-medium uppercase tracking-wide text-ink-faint">
                  {column.title}
                </h2>
                <span className="text-xs text-ink-faint">{column.cards.length}</span>
              </header>
              <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto pr-1">
                {column.cards.map((card) => (
                  <BoardCardView key={card.id} module={module} pageId={pageId} card={card} />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
