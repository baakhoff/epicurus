/**
 * The surface registry — the shell's nav is data, not markup. Later phases
 * (knowledge, calendar, bridges, installer) and module-contributed pages add
 * entries here without restructuring the shell.
 */
import type { LucideIcon } from "lucide-react";
import { Blocks, Cpu, MessageCircle, Settings } from "lucide-react";

export interface Surface {
  path: string;
  label: string;
  icon: LucideIcon;
}

export const SURFACES: Surface[] = [
  { path: "/", label: "Chat", icon: MessageCircle },
  { path: "/models", label: "Models", icon: Cpu },
  { path: "/modules", label: "Modules", icon: Blocks },
  { path: "/settings", label: "Settings", icon: Settings },
];
