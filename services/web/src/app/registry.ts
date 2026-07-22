/**
 * The surface registry — the shell's nav is data, not markup. Later phases
 * (knowledge, calendar, bridges, installer) and module-contributed pages add
 * entries here without restructuring the shell.
 */
import type { LucideIcon } from "lucide-react";
import { Activity, Bell, Blocks, Cpu, Folder, Inbox, MessageCircle, Settings, Zap } from "lucide-react";

import type { ModuleSnapshot, PageArchetype } from "@/lib/contracts";

export interface Surface {
  path: string;
  label: string;
  icon: LucideIcon;
}

/** The one surface with a live shell badge — see App.tsx's NavBadge. */
export const NOTIFICATIONS_PATH = "/notifications";

// Memory is no longer a top-level surface — it lives at the foot of Settings, since it's
// reference you curate occasionally rather than a place you visit often (ADR-0045).
export const SURFACES: Surface[] = [
  { path: "/", label: "Chat", icon: MessageCircle },
  // One inbox for every module's agent-proposed changes (#KB-refactor). The per-module review
  // pages no longer get their own nav entry — this surface aggregates them all.
  { path: "/suggestions", label: "Suggestions", icon: Inbox },
  // The durable record of every push-worthy event (#671, ADR-0102) — a core page, not a
  // module page; its shell-rendered unread badge is bespoke to this one entry (App.tsx).
  { path: "/notifications", label: "Notifications", icon: Bell },
  { path: "/models", label: "Models", icon: Cpu },
  { path: "/modules", label: "Modules", icon: Blocks },
  // The file space is a core-owned surface (ADR-0063) — the Files browser no longer
  // comes from the storage module's manifest.
  { path: "/files", label: "Files", icon: Folder },
  // Standing behavior the operator authors: what runs on its own, and how far it may go
  // (#668, ADR-0105). A core surface like Settings — the engine lives in core-app.
  { path: "/automations", label: "Automations", icon: Zap },
  { path: "/settings", label: "Settings", icon: Settings },
  { path: "/observability", label: "Observability", icon: Activity },
];

/* ── module-contributed pages (ADR-0018) ─────────────────────────────────── */

/** A left-nav entry for a module page; `icon` is a vendored glyph name. */
export interface ModulePageNav {
  path: string;
  module: string;
  pageId: string;
  label: string;
  archetype: PageArchetype;
  icon: string;
  navOrder: number;
}

/** The route a module page is rendered at. */
export function modulePagePath(moduleName: string, pageId: string): string {
  return `/m/${encodeURIComponent(moduleName)}/${encodeURIComponent(pageId)}`;
}

/**
 * Derive left-nav entries from modules' declared pages (ADR-0018). Only
 * **reachable, enabled** modules contribute — a page whose module is down can't
 * serve its data, and a disabled module (#126) is hidden from the nav while its
 * container keeps running. Sorted by each page's nav_order, then label, for a
 * stable order.
 */
export function modulePageNavs(modules: ModuleSnapshot[]): ModulePageNav[] {
  return modules
    .filter((m) => m.status.healthy && m.enabled)
    .flatMap((m) =>
      m.manifest.pages.map((page) => ({
        path: modulePagePath(m.manifest.name, page.id),
        module: m.manifest.name,
        pageId: page.id,
        label: page.title,
        archetype: page.archetype,
        icon: page.icon,
        navOrder: page.nav_order,
      })),
    )
    .sort((a, b) => a.navOrder - b.navOrder || a.label.localeCompare(b.label));
}

/**
 * The `review`-archetype pages — every module's agent-proposal queue. The unified Suggestions
 * inbox aggregates these, and the rail filters them out of its module-page list (they no longer
 * get a per-module nav entry). Derived from the same reachable+enabled modules.
 */
export function reviewPageNavs(modules: ModuleSnapshot[]): ModulePageNav[] {
  return modulePageNavs(modules).filter((p) => p.archetype === "review");
}

/**
 * Apply the operator's persisted left-nav order (#543) on top of `modulePageNavs`' default
 * order. `order` holds each page's `path` (already the unique id every caller keys off),
 * most-preferred-first. Merge semantics, by construction rather than special-casing:
 *
 * - **A page in `order`** sorts by its position there, ahead of every page that isn't.
 * - **A page not in `order`** (a newly wired module, or one reordering never touched) keeps its
 *   relative `modulePageNavs` position, appended after every ordered page — it never vanishes.
 * - **An id in `order` with no matching live page** (a stale entry — the module was removed, or
 *   the id predates a rename) is simply never looked up; it's inert, not an error.
 * - **A disabled module's page** is absent from `pages` entirely (`modulePageNavs` already
 *   filters it out), so it's untouched by this sort. Since `order` is never pruned when a page
 *   disappears, re-enabling the module restores its old position automatically — no dedicated
 *   disable/enable bookkeeping needed.
 *
 * Relies on `Array.prototype.sort`'s stability (guaranteed since ES2019): two unordered pages
 * keep the relative order `pages` already arrived in, so their own navOrder/label tiebreak
 * never needs to be recomputed here.
 */
export function sortByPageOrder(pages: ModulePageNav[], order: string[]): ModulePageNav[] {
  if (order.length === 0) return pages;
  const rank = new Map(order.map((id, i) => [id, i]));
  return [...pages].sort((a, b) => {
    const ra = rank.get(a.path);
    const rb = rank.get(b.path);
    if (ra === undefined && rb === undefined) return 0;
    if (ra === undefined) return 1;
    if (rb === undefined) return -1;
    return ra - rb;
  });
}
