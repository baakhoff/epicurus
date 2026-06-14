/**
 * The `browser` archetype (ADR-0018): a tree/list + detail view, core-rendered.
 * The module supplies only data (a list of items with detail bodies) through the
 * core page proxy; this screen renders it in ε style. No module markup runs here.
 *
 * Responsive: two panes side-by-side on wide screens; on phones the list fills the
 * view and selecting an item slides to its detail (with a back affordance).
 */
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useState } from "react";

import { EmptyState, Spinner, cn } from "@/components/ui";
import { api } from "@/lib/api";
import { BrowserData } from "@/lib/contracts";

export function BrowserView({ module, pageId }: { module: string; pageId: string }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
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

  const data = BrowserData.parse(query.data ?? {});
  const selected = data.items.find((item) => item.id === selectedId) ?? null;

  return (
    <div className="grid h-full min-h-0 sm:grid-cols-[minmax(0,20rem)_1fr]">
      {/* list pane — hidden on phone once an item is open */}
      <div
        className={cn(
          "min-h-0 overflow-y-auto border-edge sm:border-r",
          selected && "hidden sm:block",
        )}
      >
        {data.items.length === 0 ? (
          <EmptyState quote="Nothing here yet." />
        ) : (
          <ul className="flex flex-col p-2">
            {data.items.map((item) => (
              <li key={item.id}>
                <button
                  onClick={() => setSelectedId(item.id)}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-(--radius-field) px-3 py-2 text-left transition-colors",
                    item.id === selectedId
                      ? "bg-accent-dim text-accent-strong"
                      : "text-ink hover:bg-surface-2",
                  )}
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm">{item.title}</span>
                    {item.subtitle && (
                      <span className="block truncate text-xs text-ink-faint">{item.subtitle}</span>
                    )}
                  </span>
                  <ChevronRight size={15} className="shrink-0 text-ink-faint" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* detail pane — hidden on phone until an item is open */}
      <div className={cn("min-h-0 overflow-y-auto", !selected && "hidden sm:block")}>
        {selected ? (
          <article className="mx-auto max-w-2xl px-5 py-5">
            <button
              onClick={() => setSelectedId(null)}
              className="mb-3 inline-flex items-center gap-1 text-sm text-ink-dim hover:text-ink sm:hidden"
            >
              <ChevronLeft size={15} /> back
            </button>
            <h2 className="font-serif text-xl text-ink">{selected.title}</h2>
            {selected.subtitle && <p className="mt-0.5 text-sm text-ink-dim">{selected.subtitle}</p>}
            {selected.body && (
              <p className="mt-4 text-[15px] leading-relaxed whitespace-pre-wrap text-ink">
                {selected.body}
              </p>
            )}
          </article>
        ) : (
          <div className="hidden h-full items-center justify-center sm:flex">
            <EmptyState quote="Select something to read it here." />
          </div>
        )}
      </div>
    </div>
  );
}
