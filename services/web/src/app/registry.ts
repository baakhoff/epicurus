/**
 * The surface registry — the shell's nav is data, not markup. Later phases
 * (knowledge, calendar, bridges, installer) and module-contributed pages add
 * entries here without restructuring the shell.
 */
import type { LucideIcon } from "lucide-react";
import { Activity, Blocks, Brain, Cpu, MessageCircle, Settings } from "lucide-react";

import type { ModuleSnapshot, PageArchetype } from "@/lib/contracts";

export interface Surface {
  path: string;
  label: string;
  icon: LucideIcon;
}

export const SURFACES: Surface[] = [
  { path: "/", label: "Chat", icon: MessageCircle },
  { path: "/memory", label: "Memory", icon: Brain },
  { path: "/models", label: "Models", icon: Cpu },
  { path: "/modules", label: "Modules", icon: Blocks },
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
