/**
 * Memory — the durable facts epicurus remembers about you across chats (ADR-0045),
 * embedded at the foot of Settings. Facts are saved when you ask it to ("remember
 * that…") and learned automatically in the background as you talk; search what it
 * knows, and forget anything you'd rather it didn't keep. A bounded, scrollable box —
 * memory is reference you curate occasionally, not a place you visit often.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Brain, Search, Sparkles, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

import { Badge, Button, Card, Spinner, TextArea, TextInput, cn } from "@/components/ui";
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
    <div className="group/fact flex items-start gap-3 py-2.5">
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
          "hover:text-danger focus-visible:opacity-100 group-hover/fact:opacity-100 disabled:opacity-50",
        )}
      >
        {forgetting ? <Spinner className="size-3.5" /> : <Trash2 size={15} />}
      </button>
    </div>
  );
}

/**
 * The standing profile (#527) — a compact, editable picture of the user that epicurus injects at
 * the start of every chat. It is synthesized overnight from the facts below (no per-turn cost),
 * and an operator edit is *pinned*: it survives re-synthesis until cleared. Sits atop the fact
 * list — the summary above the details it's built from.
 */
function StandingProfilePanel() {
  const queryClient = useQueryClient();
  const view = useQuery({ queryKey: ["profile"], queryFn: api.profile });

  const stored = view.data?.profile?.content ?? "";
  const pinned = view.data?.pinned ?? false;
  const hasProfile = (view.data?.profile ?? null) != null;

  // Track the draft against the stored value, adjusting during render (not in an effect) so a
  // fresh synthesis / save / clear re-seeds the textarea, while the operator's own typing wins
  // until the stored value next changes — the documented "state that mirrors a prop" pattern.
  const [draft, setDraft] = useState("");
  const [lastStored, setLastStored] = useState<string | null>(null);
  if (view.data !== undefined && stored !== lastStored) {
    setLastStored(stored);
    setDraft(stored);
  }
  const dirty = draft !== stored;

  const save = useMutation({
    mutationFn: () => api.saveProfile(draft),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["profile"] }),
  });
  const clear = useMutation({
    mutationFn: api.clearProfile,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["profile"] }),
  });

  return (
    <div className="mb-4 border-b border-edge pb-4">
      <div className="mb-2 flex items-center gap-2">
        <Sparkles size={16} className="text-accent" />
        <h4 className="font-serif text-sm text-ink">Standing profile</h4>
        {hasProfile && (
          <Badge tone={pinned ? "accent" : "dim"}>{pinned ? "your edit" : "auto"}</Badge>
        )}
      </div>
      <p className="mb-2 text-xs text-ink-dim">
        A short picture of you epicurus keeps in mind every chat — written overnight from your
        facts below. Edit it to correct or add anything; your edit is kept until you clear it.
      </p>

      {view.isLoading ? (
        <div className="flex justify-center py-4">
          <Spinner />
        </div>
      ) : view.isError ? (
        <p className="py-2 text-sm text-danger">
          Couldn’t load the profile. {(view.error as Error)?.message}
        </p>
      ) : (
        <>
          <TextArea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={4}
            aria-label="Standing profile"
            placeholder="No profile yet — it’s written overnight from your facts. You can also write your own here."
          />
          <div className="mt-2 flex items-center gap-2">
            <Button
              variant="primary"
              size="sm"
              disabled={!dirty || !draft.trim()}
              busy={save.isPending}
              onClick={() => save.mutate()}
            >
              Save
            </Button>
            {hasProfile && (
              <Button
                variant="ghost"
                size="sm"
                busy={clear.isPending}
                onClick={() => clear.mutate()}
                title="Clear the profile — the next overnight run writes a fresh one from your facts"
              >
                Clear
              </Button>
            )}
            {dirty && <span className="text-[11px] text-ink-faint">Unsaved changes</span>}
          </div>
          {save.isError && (
            <p className="mt-1 text-sm text-danger">Couldn’t save. {(save.error as Error)?.message}</p>
          )}
        </>
      )}
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
      <StandingProfilePanel />
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
