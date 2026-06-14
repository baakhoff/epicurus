/**
 * The vendored icon set a module's manifest may name — for its card (ADR-0007)
 * and its left-nav pages (ADR-0018). A manifest names a glyph (never an image
 * URL or script); unknown names fall back to the puzzle piece. The shell owns
 * this set, so module visuals stay coherent with the rest of the app.
 */
import {
  Bell,
  Blocks,
  Bot,
  BookOpen,
  Calendar,
  CheckSquare,
  Cloud,
  Database,
  FileText,
  FolderOpen,
  Globe,
  Home,
  Image,
  KeyRound,
  Mail,
  MessageSquare,
  Music,
  Pencil,
  Plus,
  Puzzle,
  Rss,
  Search,
  Zap,
  type LucideIcon,
} from "lucide-react";

export const MODULE_ICONS: Record<string, LucideIcon> = {
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
  key: KeyRound,
  mail: Mail,
  message: MessageSquare,
  music: Music,
  pencil: Pencil,
  plus: Plus,
  puzzle: Puzzle,
  rss: Rss,
  search: Search,
  zap: Zap,
};

export function moduleIcon(name: string | undefined): LucideIcon {
  return (name && MODULE_ICONS[name]) || Puzzle;
}
