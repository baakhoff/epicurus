/**
 * Memory — what epicurus remembers across chats. Surfaces the cross-chat recall corpus
 * (the snippets pulled into future conversations as context): browse it newest-first,
 * search it to see exactly what surfaces for a topic, and forget any item so it stops
 * being recalled. Forgetting drops only the recall vector — the source conversation is
 * untouched (delete a whole conversation from the Chat → Conversations sheet instead).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Brain, MessageCircle, Search, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Badge, Button, Card, EmptyState, Spinner, TextInput, cn } from "@/components/ui";
import { api } from "@/lib/api";
import type { MemoryItem } from "@/lib/contracts";
import { relativeTime } from "@/lib/format";
import { useChat } from "@/stores/chat";

const QUOTE = "The memory of past joys is itself a present pleasure.";

/** Who the snippet came from — the recall corpus holds both sides of the conversation. */
function roleLabel(role: string): { label: string; tone: "accent" | "dim" } {
  if (role === "user") return { label: "You", tone: "accent" };
  if (role === "assistant") return { label: "epicurus", tone: "dim" };
  return { label: "note", tone: "dim" };
}

function MemoryRow({
  item,
  sessionTitle,
  onOpen,
  onForget,
  forgetting,
}: {
  item: MemoryItem;
  sessionTitle: string | undefined;
  onOpen: (sessionId: string) => void;
  onForget: (id: number) => void;
  forgetting: boolean;
}) {
  const role = roleLabel(item.role);
  return (
    <Card className="group">
      <div className="mb-1.5 flex items-center gap-2">
        <Badge tone={role.tone}>{role.label}</Badge>
        {item.created_at && (
          <span className="text-xs text-ink-faint">{relativeTime(item.created_at)}</span>
        )}
        {item.score != null && (
          <span className="text-xs text-ink-faint">{Math.round(item.score * 100)}% match</span>
        )}
        <button
          aria-label="Forget this memory"
          title="Forget — stop recalling this"
          onClick={() => onForget(item.id)}
          disabled={forgetting}
          className="ml-auto rounded p-1.5 text-ink-faint opacity-0 transition-opacity hover:text-danger focus-visible:opacity-100 group-hover:opacity-100 disabled:opacity-50"
        >
          {forgetting ? <Spinner className="size-3.5" /> : <Trash2 size={15} />}
        </button>
      </div>
      <p className="text-sm leading-relaxed whitespace-pre-wrap text-ink">{item.text}</p>
      <button
        onClick={() => onOpen(item.session_id)}
        className="mt-2 inline-flex items-center gap-1 text-xs text-ink-faint transition-colors hover:text-accent-strong"
      >
        <MessageCircle size={12} />
        {sessionTitle ? `from “${sessionTitle}”` : "open the conversation"}
      </button>
    </Card>
  );
}

export function MemoryScreen() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const openSession = useChat((s) => s.openSession);

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
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: api.sessions });
  const titles = useMemo(
    () => new Map((sessions.data ?? []).map((s) => [s.id, s.title])),
    [sessions.data],
  );

  const forget = useMutation({
    mutationFn: api.forgetMemory,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["memory"] }),
  });

  const openConversation = (sessionId: string) => {
    openSession(sessionId);
    navigate("/");
  };

  const items = listing.data?.items ?? [];
  const total = listing.data?.total ?? 0;
  const searching = debounced.length > 0;
  const hiddenCount = searching ? 0 : Math.max(0, total - items.length);

  return (
    <div className="flex h-full flex-col">
      {/* header */}
      <div className="flex flex-col gap-3 border-b border-edge p-4">
        <div className="flex items-center gap-2">
          <Brain size={18} className="text-accent" />
          <h1 className="font-serif text-base text-ink">Memory</h1>
          {total > 0 && (
            <span className="text-xs text-ink-faint">
              {total} {total === 1 ? "memory" : "memories"}
            </span>
          )}
        </div>
        <p className="text-sm text-ink-dim">
          What epicurus recalls across your chats — pulled in as context when it’s relevant.
          Search to see what surfaces for a topic, or forget anything you’d rather it didn’t keep.
        </p>
        <div className="relative max-w-md">
          <Search
            size={15}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint"
          />
          <TextInput
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Ask what it remembers…"
            aria-label="Search memory"
            className="pl-9"
          />
        </div>
      </div>

      {/* list */}
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <div className="mx-auto flex max-w-2xl flex-col gap-2">
          {listing.isLoading && (
            <div className="flex justify-center py-10">
              <Spinner />
            </div>
          )}

          {listing.isError && (
            <Card className="border-danger/40 text-sm text-danger">
              Couldn’t reach memory. {(listing.error as Error)?.message}
            </Card>
          )}

          {listing.isSuccess && items.length === 0 && (
            <EmptyState quote={searching ? undefined : QUOTE}>
              <p className="text-sm text-ink-dim">
                {searching
                  ? `Nothing recalled for “${debounced}”.`
                  : "Nothing remembered yet — your conversations will gather here."}
              </p>
            </EmptyState>
          )}

          {items.map((item) => (
            <MemoryRow
              key={item.id}
              item={item}
              sessionTitle={titles.get(item.session_id)}
              onOpen={openConversation}
              onForget={(id) => forget.mutate(id)}
              forgetting={forget.isPending && forget.variables === item.id}
            />
          ))}

          {hiddenCount > 0 && (
            <p className={cn("py-2 text-center text-xs text-ink-faint")}>
              Showing the {items.length} most recent · {hiddenCount} more — search to find them.
            </p>
          )}

          {forget.isError && (
            <Card className="border-danger/40 text-sm text-danger">
              Couldn’t forget that one. {(forget.error as Error)?.message}
              <div className="mt-2">
                <Button variant="outline" onClick={() => forget.reset()}>
                  Dismiss
                </Button>
              </div>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
