/**
 * The in-app notification center (#671, ADR-0102) — the durable record of every
 * push-worthy event, independent of whether push itself delivered (a quiet-hours-held or
 * unsubscribed-device push still lands here immediately). A core page (ADR-0018/0019), not
 * a module page: aggregates across every category, the same "shell renders, no module UI"
 * shape as Suggestions — the closest existing precedent, though this feed has no per-module
 * review toggle to render, just categories and read state.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, CheckCheck } from "lucide-react";
import { useState } from "react";

import { CardLink } from "@/components/CardLink";
import { EntityRefChip } from "@/components/EntityRef";
import { Badge, EmptyState, Select, Spinner, Switch, cn } from "@/components/ui";
import { api } from "@/lib/api";
import type { NotificationCenterItem } from "@/lib/contracts";

function notificationsKey(filter: { category?: string; unreadOnly?: boolean }) {
  return ["notifications", filter.category ?? null, filter.unreadOnly ?? false] as const;
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

/** One row: category + title + body + timestamp, an unread dot, and any entity ref / deep
 *  link the notification carries. Clicking anywhere marks it read — the standard
 *  notification-inbox convention (GitHub, Slack, Gmail); re-clicking an already-read row is
 *  a no-op rather than a repeat network call. */
function NotificationRow({ item }: { item: NotificationCenterItem }) {
  const qc = useQueryClient();
  const unread = item.read_at === null;
  const markRead = useMutation({
    mutationFn: () => api.markNotificationRead(item.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["notifications"] });
      void qc.invalidateQueries({ queryKey: ["notifications-unread-count"] });
    },
  });

  return (
    <li
      onClick={() => unread && markRead.mutate()}
      className={cn(
        "flex flex-col gap-2 rounded-(--radius-card) border p-3",
        unread ? "cursor-pointer border-accent/30 bg-accent-dim/40" : "border-edge bg-surface",
      )}
    >
      <div className="flex items-center gap-2">
        {unread && (
          <span className="size-1.5 shrink-0 rounded-full bg-accent" aria-label="Unread" />
        )}
        <Badge tone="dim" className="shrink-0 capitalize">
          {item.category}
        </Badge>
        <p
          className={cn(
            "min-w-0 flex-1 truncate text-sm",
            unread ? "font-medium text-ink" : "text-ink-dim",
          )}
        >
          {item.title}
        </p>
        <span className="shrink-0 text-xs text-ink-faint">{formatWhen(item.created_at)}</span>
      </div>
      {item.body && <p className="text-sm text-ink-dim">{item.body}</p>}
      {(item.entity_ref || item.deep_link) && (
        <div className="flex items-center gap-3">
          {item.entity_ref && <EntityRefChip entref={item.entity_ref} />}
          {item.deep_link && (
            <CardLink
              href={{ label: "Open", url: item.deep_link }}
              className="text-xs text-accent-strong"
            />
          )}
        </div>
      )}
    </li>
  );
}

export function NotificationsScreen() {
  const qc = useQueryClient();
  const [category, setCategory] = useState("");
  const [unreadOnly, setUnreadOnly] = useState(false);
  // known_categories is the same platform-owned taxonomy Settings → Push notifications
  // renders toggles for (ADR-0102 §4) — reused here rather than duplicated.
  const prefs = useQuery({ queryKey: ["push-prefs"], queryFn: api.pushPrefs });
  const filter = { category: category || undefined, unreadOnly };
  const items = useQuery({
    queryKey: notificationsKey(filter),
    queryFn: () => api.notifications(filter),
  });

  const markAllRead = useMutation({
    mutationFn: api.markAllNotificationsRead,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["notifications"] });
      void qc.invalidateQueries({ queryKey: ["notifications-unread-count"] });
    },
  });

  const rows = items.data ?? [];
  const hasUnread = rows.some((n) => n.read_at === null);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-3 border-b border-edge px-4 py-2.5">
        <Bell size={16} className="shrink-0 text-ink-faint" />
        <h1 className="font-serif text-base text-ink">Notifications</h1>
        <div className="ml-auto flex items-center gap-3">
          <Select
            size="sm"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            aria-label="Filter by category"
          >
            <option value="">All categories</option>
            {(prefs.data?.known_categories ?? []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.label}
              </option>
            ))}
          </Select>
          <div className="flex items-center gap-1.5">
            <Switch checked={unreadOnly} onChange={setUnreadOnly} label="Show unread only" />
            <span className="text-xs text-ink-dim">Unread only</span>
          </div>
          {hasUnread && (
            <button
              onClick={() => markAllRead.mutate()}
              disabled={markAllRead.isPending}
              className="flex items-center gap-1.5 text-xs text-ink-dim hover:text-ink disabled:opacity-50"
            >
              <CheckCheck size={13} />
              Mark all read
            </button>
          )}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {items.isLoading ? (
          <div className="flex h-full items-center justify-center">
            <Spinner />
          </div>
        ) : items.isError ? (
          <div className="flex h-full items-center justify-center p-6">
            <p className="text-sm text-danger">Could not load notifications.</p>
          </div>
        ) : rows.length === 0 ? (
          <div className="flex h-full items-center justify-center p-6">
            <EmptyState quote="Nothing here yet.">
              <p className="flex items-center gap-2 text-sm text-ink-dim">
                <Bell size={15} />
                Notifications land here the moment something happens — read or not.
              </p>
            </EmptyState>
          </div>
        ) : (
          <ul className="mx-auto flex max-w-2xl flex-col gap-2 p-4">
            {rows.map((n) => (
              <NotificationRow key={n.id} item={n} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
