/**
 * Module page host (ADR-0018). A module-contributed left-nav page resolves to one
 * core-rendered archetype screen — the module never ships markup. This screen reads
 * the page's archetype from the module manifest and dispatches to the matching
 * first-party view; unknown/not-yet-built archetypes degrade to a tasteful notice.
 */
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import { BrowserView } from "@/components/archetypes/BrowserView";
import { CalendarView } from "@/components/archetypes/CalendarView";
import { EmptyState, Spinner } from "@/components/ui";
import { api } from "@/lib/api";
import type { PageArchetype } from "@/lib/contracts";

function ComingSoon({ archetype }: { archetype: PageArchetype }) {
  return (
    <EmptyState quote="This view is still being built.">
      <p className="text-sm text-ink-dim">
        The <span className="font-mono">{archetype}</span> view arrives with its module page.
      </p>
    </EmptyState>
  );
}

export function ModulePageScreen() {
  const { moduleName = "", pageId = "" } = useParams();
  const modules = useQuery({ queryKey: ["modules"], queryFn: api.modules });

  if (modules.isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner />
      </div>
    );
  }

  const snapshot = modules.data?.find((m) => m.manifest.name === moduleName);
  const page = snapshot?.manifest.pages.find((p) => p.id === pageId);

  if (!snapshot || !page) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <EmptyState quote="That page wandered off.">
          <p className="text-sm text-ink-dim">No such module page.</p>
        </EmptyState>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-edge px-4 py-2.5">
        <h1 className="font-serif text-base text-ink">{page.title}</h1>
        <span className="text-xs text-ink-faint">{snapshot.manifest.name}</span>
      </div>
      <div className="min-h-0 flex-1">
        {page.archetype === "browser" ? (
          <BrowserView module={moduleName} pageId={pageId} />
        ) : page.archetype === "calendar" ? (
          <CalendarView module={moduleName} pageId={pageId} />
        ) : (
          <div className="flex h-full items-center justify-center p-6">
            <ComingSoon archetype={page.archetype} />
          </div>
        )}
      </div>
    </div>
  );
}
