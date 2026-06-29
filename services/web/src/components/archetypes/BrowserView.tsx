/**
 * The `browser` archetype (ADR-0018): a tree/list + detail view, core-rendered.
 * The module supplies only data (a list of items with detail bodies) through the
 * core page proxy; this screen renders it in ε style. No module markup runs here.
 *
 * Extensions over the base contract:
 *  - `search_enabled` → renders a search input; query param `q` is forwarded.
 *  - `path` → current directory path; breadcrumbs let the user navigate up.
 *  - `nav_path` on an item → clicking drills into that directory (sets path param).
 *  - `href` on an item → a download link is shown in the detail pane.
 *  - `movable` on an item → the entry can be renamed (detail pane) and dragged onto a
 *    folder/breadcrumb to move it (#391), through the shared `/pages/{id}/move` contract.
 *
 * Responsive: two panes side-by-side on wide screens; on phones the list fills the
 * view and selecting an item slides to its detail (with a back affordance).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowUp,
  BookOpen,
  Check,
  ChevronLeft,
  ChevronRight,
  Download,
  Folder,
  Pencil,
  Search,
  X,
} from "lucide-react";
import { useRef, useState } from "react";

import { Button, EmptyState, Spinner, TextInput, Tooltip, cn } from "@/components/ui";
import { ApiError } from "@/lib/api";
import { BrowserData, type BrowserItem } from "@/lib/contracts";
import { usePanel } from "@/stores/panel";

/**
 * A data adapter the `browser` archetype renders against (ADR-0063). It decouples the
 * view from where its data lives: a module page (the core page proxy) or a core-owned
 * surface (e.g. the Files screen). The view appends `[currentPath, activeQuery]` to
 * `queryKey` for caching and invalidates `queryKey` after a move.
 */
export interface BrowserSource {
  /** Base react-query key; the view appends `[currentPath, activeQuery]`. */
  queryKey: unknown[];
  /** Fetch a directory/search listing — returns BrowserData-shaped json. */
  fetchPage: (path: string, q: string) => Promise<unknown>;
  /** Read a text file's contents for the split-screen reader. */
  readText: (path: string) => Promise<{ path: string; name: string; content: string }>;
  /** Move or rename an item (`from` → `to`). */
  move: (from: string, to: string) => Promise<unknown>;
}

/** File extensions the Files browser can open inline in the split-screen reader (req 6). */
const TEXT_EXT =
  /\.(md|markdown|mdx|txt|text|log|json|ya?ml|toml|ini|cfg|conf|csv|tsv|xml|html?|css|s?css|jsx?|tsx?|py|rb|go|rs|java|kt|c|h|cpp|sh|bash|sql|env)$/i;

function isTextFile(name: string): boolean {
  return TEXT_EXT.test(name);
}

/** Breadcrumb segment for directory navigation. */
interface Crumb {
  label: string;
  path: string;
}

function crumbs(path: string): Crumb[] {
  if (!path) return [];
  const parts = path.split("/").filter(Boolean);
  return parts.map((label, i) => ({ label, path: parts.slice(0, i + 1).join("/") }));
}

/** The parent directory of `path` (one level up); `""` (root) for a top-level dir or root. */
function parentPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  parts.pop();
  return parts.join("/");
}

/** The last segment of a path — the display/file name. */
function basename(path: string): string {
  return path.split("/").filter(Boolean).pop() ?? path;
}

/** Join a directory and a name into a POSIX path (`""` dir = root). */
function joinPath(dir: string, name: string): string {
  return dir ? `${dir}/${name}` : name;
}

function ItemIcon({ item }: { item: BrowserItem }) {
  if (item.nav_path) return <Folder size={15} className="shrink-0 text-ink-faint" />;
  return null;
}

export function BrowserView({ source }: { source: BrowserSource }) {
  const [currentPath, setCurrentPath] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [activeQuery, setActiveQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // Inline rename (detail pane) and native drag-to-move (#391).
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  const [dragId, setDragId] = useState<string | null>(null);
  const [dragOverPath, setDragOverPath] = useState<string | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const qc = useQueryClient();

  // Open a text file in the right-panel split-screen reader (#KB-refactor, req 6).
  const panelOpen = usePanel((s) => s.open);
  const openInPanel = useMutation({
    mutationFn: (path: string) => source.readText(path),
    onSuccess: (file) => panelOpen("doc-reader", file, file.name),
    onError: (err) =>
      window.alert(
        err instanceof ApiError ? `Could not open: ${err.detail}` : "Could not open this file.",
      ),
  });

  // Rename / move through the shared move contract; refetch the listing on success (#391).
  const move = useMutation({
    mutationFn: ({ from, to }: { from: string; to: string }) => source.move(from, to),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: source.queryKey });
      setSelectedId(null);
      setRenaming(false);
    },
    onError: (err) =>
      window.alert(
        err instanceof ApiError ? `Could not move: ${err.detail}` : "Could not move this item.",
      ),
  });

  // Reset selection when the path or query changes — adjust state during render
  // (the React-blessed alternative to a setState-in-effect, no extra commit).
  const navKey = JSON.stringify([currentPath, activeQuery]);
  const [lastNavKey, setLastNavKey] = useState(navKey);
  if (navKey !== lastNavKey) {
    setLastNavKey(navKey);
    setSelectedId(null);
    setRenaming(false);
  }

  const query = useQuery({
    queryKey: [...source.queryKey, currentPath, activeQuery],
    queryFn: () => source.fetchPage(currentPath, activeQuery),
  });

  if (query.isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <EmptyState quote="This page is resting.">
          <p className="text-sm text-ink-dim">{(query.error as Error).message}</p>
        </EmptyState>
      </div>
    );
  }

  const data = BrowserData.parse(query.data ?? {});
  const selected = data.items.find((item) => item.id === selectedId) ?? null;
  const breadcrumbs = crumbs(currentPath);

  function navigateTo(path: string) {
    setCurrentPath(path);
    setSearchInput("");
    setActiveQuery("");
  }

  function submitSearch(e: React.FormEvent) {
    e.preventDefault();
    setActiveQuery(searchInput.trim());
    setCurrentPath("");
  }

  function clearSearch() {
    setSearchInput("");
    setActiveQuery("");
    searchRef.current?.focus();
  }

  // Whether `src` may be moved into directory `targetDir` — not into itself/a descendant,
  // and not a no-op back into its current parent. The backend re-checks authoritatively.
  function canDropInto(src: string, targetDir: string): boolean {
    if (src === targetDir || targetDir.startsWith(`${src}/`)) return false;
    return parentPath(src) !== targetDir;
  }

  function dropInto(targetDir: string) {
    const src = dragId;
    setDragId(null);
    setDragOverPath(null);
    if (src && canDropInto(src, targetDir)) {
      move.mutate({ from: src, to: joinPath(targetDir, basename(src)) });
    }
  }

  function submitRename(e: React.FormEvent) {
    e.preventDefault();
    if (!selected) return;
    const name = renameValue.trim();
    if (!name || name === selected.title) {
      setRenaming(false);
      return;
    }
    move.mutate({ from: selected.id, to: joinPath(parentPath(selected.id), name) });
  }

  // Drop-target props shared by folder rows and breadcrumb segments.
  function dropProps(targetDir: string) {
    return {
      onDragOver: (e: React.DragEvent) => {
        if (!dragId || !canDropInto(dragId, targetDir)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move" as const;
        setDragOverPath(targetDir);
      },
      onDragLeave: () => setDragOverPath((p) => (p === targetDir ? null : p)),
      onDrop: (e: React.DragEvent) => {
        e.preventDefault();
        dropInto(targetDir);
      },
    };
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* toolbar: breadcrumbs + optional search */}
      {(breadcrumbs.length > 0 || data.search_enabled) && (
        <div className="flex shrink-0 items-center gap-2 border-b border-edge px-3 py-1.5">
          {/* breadcrumbs */}
          {breadcrumbs.length > 0 && (
            <nav className="flex min-w-0 flex-1 items-center gap-1 text-xs text-ink-dim">
              {/* Up one level (#338): jump to the parent of the current directory. */}
              <Tooltip label="Up one level" side="bottom">
                <button
                  onClick={() => navigateTo(parentPath(currentPath))}
                  className="-ml-1 shrink-0 rounded-(--radius-field) p-1 hover:bg-surface-2 hover:text-ink"
                  aria-label="Up one level"
                >
                  <ArrowUp size={13} />
                </button>
              </Tooltip>
              <button
                onClick={() => navigateTo("")}
                className={cn(
                  "hover:text-ink shrink-0 rounded-(--radius-field) px-1",
                  dragOverPath === "" && "bg-accent-dim text-accent-strong",
                )}
                aria-label="root"
                {...dropProps("")}
              >
                /
              </button>
              {breadcrumbs.map((crumb) => (
                <span key={crumb.path} className="flex items-center gap-1">
                  <ChevronRight size={12} className="shrink-0" />
                  <button
                    onClick={() => navigateTo(crumb.path)}
                    className={cn(
                      "max-w-[10rem] truncate rounded-(--radius-field) px-1 hover:text-ink",
                      dragOverPath === crumb.path && "bg-accent-dim text-accent-strong",
                    )}
                    {...dropProps(crumb.path)}
                  >
                    {crumb.label}
                  </button>
                </span>
              ))}
            </nav>
          )}

          {/* search */}
          {data.search_enabled && (
            <form onSubmit={submitSearch} className="flex items-center gap-1 ml-auto">
              <div className="relative flex items-center">
                <Search size={13} className="pointer-events-none absolute left-2 text-ink-faint" />
                <TextInput
                  ref={searchRef}
                  value={searchInput}
                  onChange={(e) => setSearchInput(e.target.value)}
                  placeholder="Search…"
                  className="h-7 w-44 pl-6 pr-7 text-xs"
                />
                {(searchInput || activeQuery) && (
                  <button
                    type="button"
                    onClick={clearSearch}
                    className="absolute right-1.5 text-ink-faint hover:text-ink"
                    aria-label="clear search"
                  >
                    <X size={12} />
                  </button>
                )}
              </div>
            </form>
          )}
        </div>
      )}

      {/* two-pane body */}
      <div className="grid min-h-0 flex-1 sm:grid-cols-[minmax(0,20rem)_1fr]">
        {/* list pane — hidden on phone once an item is open */}
        <div
          className={cn(
            "min-h-0 overflow-y-auto border-edge sm:border-r",
            selected && "hidden sm:block",
          )}
        >
          {data.items.length === 0 ? (
            <EmptyState quote={activeQuery ? "No matches." : "Nothing here yet."} />
          ) : (
            <ul className="flex flex-col p-2">
              {data.items.map((item) => {
                const isFolder = !!item.nav_path;
                const dropTarget = isFolder && !!dragId && dragId !== item.id;
                return (
                  <li key={item.id}>
                    <button
                      draggable={!!item.movable}
                      onDragStart={(e) => {
                        e.dataTransfer.effectAllowed = "move";
                        e.dataTransfer.setData("text/plain", item.id);
                        setDragId(item.id);
                      }}
                      onDragEnd={() => {
                        setDragId(null);
                        setDragOverPath(null);
                      }}
                      {...(isFolder ? dropProps(item.nav_path as string) : {})}
                      onClick={() => {
                        if (item.nav_path) {
                          navigateTo(item.nav_path);
                        } else {
                          setSelectedId(item.id);
                        }
                      }}
                      className={cn(
                        "flex w-full items-center gap-2 rounded-(--radius-field) px-3 py-2 text-left transition-colors",
                        item.id === selectedId
                          ? "bg-accent-dim text-accent-strong"
                          : "text-ink hover:bg-surface-2",
                        dropTarget &&
                          dragOverPath === item.nav_path &&
                          "bg-accent-dim ring-1 ring-accent ring-inset",
                        item.movable && "cursor-grab active:cursor-grabbing",
                      )}
                    >
                      <ItemIcon item={item} />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm">{item.title}</span>
                        {item.subtitle && (
                          <span className="block truncate text-xs text-ink-faint">
                            {item.subtitle}
                          </span>
                        )}
                      </span>
                      <ChevronRight size={15} className="shrink-0 text-ink-faint" />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {/* detail pane — hidden on phone until an item is open */}
        <div className={cn("min-h-0 overflow-y-auto", !selected && "hidden sm:block")}>
          {selected ? (
            <article className="mx-auto max-w-2xl px-5 py-5">
              <button
                onClick={() => setSelectedId(null)}
                className="mb-3 inline-flex items-center gap-1 text-sm text-ink-dim hover:text-ink sm:hidden"
              >
                <ChevronLeft size={15} /> back
              </button>
              {renaming ? (
                <form onSubmit={submitRename} className="flex items-center gap-2">
                  <TextInput
                    autoFocus
                    value={renameValue}
                    onChange={(e) => setRenameValue(e.target.value)}
                    onKeyDown={(e) => e.key === "Escape" && setRenaming(false)}
                    className="min-w-0 flex-1 rounded-(--radius-field) border border-edge bg-surface-1 px-2 py-1 font-serif text-xl text-ink focus:outline-none focus:ring-1 focus:ring-accent"
                    aria-label="New name"
                  />
                  <Button type="submit" variant="primary" busy={move.isPending}>
                    <Check size={14} /> Save
                  </Button>
                  <Button type="button" variant="ghost" onClick={() => setRenaming(false)}>
                    Cancel
                  </Button>
                </form>
              ) : (
                <div className="flex items-start gap-2">
                  <h2 className="min-w-0 flex-1 font-serif text-xl text-ink">{selected.title}</h2>
                  {selected.movable && (
                    <Tooltip label="Rename" side="bottom">
                      <button
                        onClick={() => {
                          setRenameValue(selected.title);
                          setRenaming(true);
                        }}
                        className="shrink-0 rounded-(--radius-field) p-1.5 text-ink-faint hover:bg-surface-2 hover:text-ink"
                        aria-label="Rename"
                      >
                        <Pencil size={15} />
                      </button>
                    </Tooltip>
                  )}
                </div>
              )}
              {selected.subtitle && (
                <p className="mt-0.5 text-sm text-ink-dim">{selected.subtitle}</p>
              )}
              {selected.movable && !renaming && (
                <p className="mt-2 text-xs text-ink-faint">
                  Drag onto a folder or breadcrumb to move it.
                </p>
              )}
              {selected.body && (
                <p className="mt-4 text-[15px] leading-relaxed whitespace-pre-wrap text-ink">
                  {selected.body}
                </p>
              )}
              {selected.href && (
                <div className="mt-5 flex flex-wrap items-center gap-2">
                  {isTextFile(selected.title) && (
                    <Button
                      variant="primary"
                      busy={openInPanel.isPending}
                      onClick={() => openInPanel.mutate(selected.id)}
                    >
                      <BookOpen size={14} /> Open
                    </Button>
                  )}
                  <a
                    href={selected.href}
                    download={selected.title}
                    className="inline-flex items-center gap-2 rounded-(--radius-field) border border-edge bg-surface-2 px-3 py-1.5 text-sm text-ink hover:bg-surface-3 transition-colors"
                  >
                    <Download size={14} />
                    Download
                  </a>
                </div>
              )}
            </article>
          ) : (
            <div className="hidden h-full items-center justify-center sm:flex">
              <EmptyState quote="Select a file to see its details." />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
