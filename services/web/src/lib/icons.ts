/**
 * The vendored icon set a module's manifest may name — for its card (ADR-0007)
 * and its left-nav pages (ADR-0018). A manifest names a glyph (never an image
 * URL or script); unknown names fall back to the puzzle piece. The shell owns
 * this set, so module visuals stay coherent with the rest of the app.
 */
import {
  Archive,
  Bell,
  Blocks,
  Bot,
  BookOpen,
  Boxes,
  Brain,
  Calendar,
  CheckSquare,
  Cloud,
  Database,
  Eye,
  FileText,
  FolderOpen,
  Globe,
  Home,
  Image,
  Inbox,
  KeyRound,
  Mail,
  MessageSquare,
  Music,
  Pencil,
  Plus,
  Puzzle,
  RotateCcw,
  Rss,
  Search,
  Trash2,
  Wrench,
  Zap,
  type LucideIcon,
} from "lucide-react";

export const MODULE_ICONS: Record<string, LucideIcon> = {
  archive: Archive,
  bell: Bell,
  blocks: Blocks,
  bot: Bot,
  book: BookOpen,
  calendar: Calendar,
  check: CheckSquare,
  cloud: Cloud,
  database: Database,
  file: FileText,
  folder: FolderOpen,
  globe: Globe,
  home: Home,
  image: Image,
  inbox: Inbox,
  key: KeyRound,
  mail: Mail,
  message: MessageSquare,
  music: Music,
  pencil: Pencil,
  plus: Plus,
  puzzle: Puzzle,
  rotate: RotateCcw,
  rss: Rss,
  search: Search,
  trash: Trash2,
  zap: Zap,
};

export function moduleIcon(name: string | undefined): LucideIcon {
  return (name && MODULE_ICONS[name]) || Puzzle;
}

/**
 * Model capabilities worth surfacing as a badge, with a glyph + label. The runtime also
 * reports plumbing ("completion", "insert") which we don't badge — only keys present here
 * are shown, in this order.
 */
export const CAPABILITY_META: Record<string, { label: string; icon: LucideIcon }> = {
  tools: { label: "Tools", icon: Wrench },
  vision: { label: "Vision", icon: Eye },
  thinking: { label: "Thinking", icon: Brain },
  embedding: { label: "Embedding", icon: Boxes },
};

/** The subset of `caps` we badge in the UI — known capabilities only, in a stable order. */
export function shownCapabilities(caps: string[]): string[] {
  return Object.keys(CAPABILITY_META).filter((c) => caps.includes(c));
}
