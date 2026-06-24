/**
 * Memory — the durable facts epicurus remembers about you across chats (ADR-0045),
 * embedded at the foot of Settings. Facts are saved when you ask it to ("remember
 * that…") and learned automatically in the background as you talk; search what it
 * knows, and forget anything you'd rather it didn't keep. A bounded, scrollable box —
 * memory is reference you curate occasionally, not a place you visit often.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Brain, Search, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

import { Badge, Card, Spinner, TextInput, cn } from "@/components/ui";
import { api } from "@/lib/api";
import type { MemoryItem } from "@/lib/contracts";
import { relativeTime } from "@/lib/format";

/** How a fact was learned — a small provenance badge. */
function source(item: MemoryItem): { label: string; tone: "accent" | "dim" } {
  return item.source === "tool"
    ? { label: "you asked", tone: "accent" }
    : { label: "learned", tone: "dim" };
}

function FactRow({
  item,
  onForget,
  forgetting,
}: {
  item: MemoryItem;
  onForget: (id: string) => void;
  forgetting: boolean;
}) {
  const src = source(item);
  return (
    <div className="group flex items-start gap-3 py-2.5">
      <div className="min-w-0 flex-1">
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink">{item.text}</p>
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <Badge tone={src.tone}>{src.label}</Badge>
          {item.created_at && (
            <span className="text-[11px] text-ink-faint">{relativeTime(item.created_at)}</span>
          )}
          {item.score != null && (
            <span className="text-[11px] text-ink-faint">{Math.round(item.score * 100)}% match</span>
          )}
        </div>
      </div>
      <button
        aria-label="Forget this"
        title="Forget — stop remembering this"
        onClick={() => onForget(item.id)}
        disabled={forgetting}
        className={cn(
          "rounded p-1.5 text-ink-faint opacity-0 transition-opacity",
          "hover:text-danger focus-visible:opacity-100 group-hover:opacity-100 disabled:opacity-50",
        )}
      >
        {forgetting ? <Spinner className="size-3.5" /> : <Trash2 size={15} />}
      </button>
    </div>
  );
}

export function MemorySection() {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  // Debounce typing so each keystroke doesn't fire a recall search.
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query.trim()), 250);
    return () => clearTimeout(timer);
  }, [query]);

  const listing = useQuery({
    queryKey: ["memory", debounced],
    queryFn: () => api.memory(debounced || undefined),
  });
  const forget = useMutation({
    mutationFn: api.forgetMemory,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["memory"] }),
  });

  const items = listing.data?.items ?? [];
  const total = listing.data?.total ?? 0;
  const searching = debounced.length > 0;
  const hiddenCount = searching ? 0 : Math.max(0, total - items.length);

  return (
    <Card>
      <div className="mb-2 flex items-center gap-2">
        <Brain size={17} className="text-accent" />
        <h3 className="font-serif text-base text-ink">Memory</h3>
        {total > 0 && (
          <span className="text-xs text-ink-faint">
            {total} {total === 1 ? "fact" : "facts"}
          </span>
        )}
      </div>
      <p className="mb-3 text-sm text-ink-dim">
        Durable facts epicurus remembers about you across chats — saved when you ask it to, and
        learned on its own as you talk. Search what it knows, or forget anything you’d rather it
        didn’t keep.
      </p>

      <div className="relative mb-1">
        <Search
          size={15}
          className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint"
        />
        <TextInput
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search what it remembers…"
          aria-label="Search memory"
          className="pl-9"
        />
      </div>

      {listing.isLoading ? (
        <div className="flex justify-center py-8">
          <Spinner />
        </div>
      ) : listing.isError ? (
        <p className="py-3 text-sm text-danger">
          Couldn’t reach memory. {(listing.error as Error)?.message}
        </p>
      ) : items.length === 0 ? (
        <p className="py-6 text-center text-sm text-ink-dim">
          {searching
            ? `Nothing remembered for “${debounced}”.`
            : "Nothing remembered yet — epicurus gathers facts about you as you chat, or tell it to “remember” something."}
        </p>
      ) : (
        <div className="mt-2 max-h-80 divide-y divide-edge overflow-y-auto overscroll-contain">
          {items.map((item) => (
            <FactRow
              key={item.id}
              item={item}
              onForget={(id) => forget.mutate(id)}
              forgetting={forget.isPending && forget.variables === item.id}
            />
          ))}
        </div>
      )}

      {hiddenCount > 0 && (
        <p className="pt-2 text-center text-xs text-ink-faint">
          Showing the {items.length} most recent · {hiddenCount} more — search to find them.
        </p>
      )}
      {forget.isError && (
        <p className="mt-2 text-sm text-danger">
          Couldn’t forget that one. {(forget.error as Error)?.message}
        </p>
      )}
    </Card>
  );
}
