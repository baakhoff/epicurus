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
  Camera,
  Check,
  ChevronLeft,
  ChevronRight,
  Download,
  FileText,
  Folder,
  Image,
  Loader2,
  Pencil,
  Search,
  Upload,
  X,
} from "lucide-react";
import { useRef, useState } from "react";

import { Button, EmptyState, Sheet, Spinner, TextInput, Tooltip, cn } from "@/components/ui";
import { ApiError } from "@/lib/api";
import { BrowserData, type BrowserItem } from "@/lib/contracts";
import { usePanel } from "@/stores/panel";
import { toast } from "@/stores/toasts";

/**
 * A data adapter the `browser` archetype renders against (ADR-0063). It decouples the
 * view from where its data lives: a module page (the core page proxy) or a core-owned
 * surface (e.g. the Files screen). The view appends `[currentPath, activeQuery]` to
 * `queryKey` for caching and invalidates `queryKey` after a move.
 */
/**
 * Optional upload capability for a browser surface (#479). When present, the toolbar
 * offers **Upload** into the current directory — behind a camera / gallery / document
 * source menu on phones, straight to the file dialog on wide screens — and the listing
 * accepts external file drops. `send` uploads ONE file into `dir` (the view sequences a
 * multi-pick itself, for per-file progress) and throws `ApiError` on rejection, so a
 * 413/415 from the #175 caps renders as that file's failure, never a raw request error.
 */
export interface BrowserUpload {
  send: (file: File, dir: string) => Promise<unknown>;
}

export interface BrowserSource {
  /** Base react-query key; the view appends `[currentPath, activeQuery]`. */
  queryKey: unknown[];
  /** Fetch a directory/search listing — returns BrowserData-shaped json. */
  fetchPage: (path: string, q: string) => Promise<unknown>;
  /** Read a text file's contents for the split-screen reader. */
  readText: (path: string) => Promise<{ path: string; name: string; content: string }>;
  /** Move or rename an item (`from` → `to`). */
  move: (from: string, to: string) => Promise<unknown>;
  /** Uploads into the surface (#479) — the core Files screen wires this; module pages don't. */
  upload?: BrowserUpload;
}

/** One in-flight/settled upload rendered as a pill above the listing (#479). */
interface UploadItem {
  key: string;
  name: string;
  status: "sending" | "done" | "error";
  detail?: string;
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
  // Upload state (#479): the pill strip, the phone source menu, and the hidden pickers.
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [sourceMenuOpen, setSourceMenuOpen] = useState(false);
  const [fileDragOver, setFileDragOver] = useState(false);
  const uploadSeq = useRef(0);
  const galleryRef = useRef<HTMLInputElement>(null);
  const cameraRef = useRef<HTMLInputElement>(null);
  const documentRef = useRef<HTMLInputElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  // Guards a folder tap against a double-fired click (touchend + click on Android
  // PWA, #428) — a real second tap on a *different* row is never this close together.
  const lastNavRef = useRef(0);
  const qc = useQueryClient();

  // Open a text file in the right-panel split-screen reader (#KB-refactor, req 6).
  const panelOpen = usePanel((s) => s.open);
  const openInPanel = useMutation({
    mutationFn: (path: string) => source.readText(path),
    onSuccess: (file) => panelOpen("doc-reader", file, file.name),
    onError: (err) =>
      toast.error(
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
      toast.error(
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

  // Send picked/dropped files one at a time into the directory the user is looking at —
  // sequential, so each pill settles on its own and a failure names its own file (#479).
  async function sendFiles(files: File[]) {
    const upload = source.upload;
    if (!upload || files.length === 0) return;
    const dir = currentPath; // capture: navigation during the batch must not redirect it
    const batch = files.map((file) => ({
      file,
      key: String(++uploadSeq.current),
    }));
    setUploads((u) => [
      ...u,
      ...batch.map(({ file, key }) => ({ key, name: file.name, status: "sending" as const })),
    ]);
    for (const { file, key } of batch) {
      try {
        await upload.send(file, dir);
        setUploads((u) => u.map((it) => (it.key === key ? { ...it, status: "done" } : it)));
        // The new entry must show with no reload — refresh the listing per success.
        void qc.invalidateQueries({ queryKey: source.queryKey });
        window.setTimeout(() => {
          setUploads((u) => u.filter((it) => !(it.key === key && it.status === "done")));
        }, 2500);
      } catch (err) {
        const detail = err instanceof ApiError ? err.detail : "upload failed";
        setUploads((u) =>
          u.map((it) => (it.key === key ? { ...it, status: "error", detail } : it)),
        );
        toast.error(`Could not upload ${file.name}: ${detail}`);
      }
    }
  }

  function onPicked(e: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? []);
    e.target.value = ""; // allow re-picking the same file
    void sendFiles(files);
  }

  function requestUpload() {
    // Phones get the source menu (photo/video, camera, document — the pickers behind it
    // open the matching native surface); wide screens go straight to the file dialog.
    // jsdom has no matchMedia — treat that as wide.
    const wide =
      typeof window.matchMedia === "function"
        ? window.matchMedia("(min-width: 640px)").matches
        : true;
    if (wide) documentRef.current?.click();
    else setSourceMenuOpen(true);
  }

  // External file drops upload into the current directory (#479). Internal move-drags
  // (dragId set) keep their own row/breadcrumb targets — those don't carry "Files".
  const externalDropProps = source.upload
    ? {
        onDragOver: (e: React.DragEvent) => {
          if (dragId || !e.dataTransfer.types.includes("Files")) return;
          e.preventDefault();
          e.dataTransfer.dropEffect = "copy" as const;
          setFileDragOver(true);
        },
        onDragLeave: () => setFileDragOver(false),
        onDrop: (e: React.DragEvent) => {
          if (dragId || e.defaultPrevented) return; // a row target already took it
          e.preventDefault();
          setFileDragOver(false);
          void sendFiles(Array.from(e.dataTransfer.files));
        },
      }
    : {};

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
      {/* toolbar: breadcrumbs + optional search + optional upload */}
      {(breadcrumbs.length > 0 || data.search_enabled || source.upload) && (
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

          {/* upload into the current directory (#479) */}
          {source.upload && (
            <>
              <Button
                size="sm"
                onClick={requestUpload}
                className={cn("shrink-0", !data.search_enabled && "ml-auto")}
                aria-label="Upload"
              >
                <Upload size={13} /> Upload
              </Button>
              {/* eslint-disable-next-line no-restricted-syntax -- hidden file input (#394 carve-out): the gallery picker behind the Upload affordance */}
              <input
                ref={galleryRef}
                type="file"
                accept="image/*,video/*"
                multiple
                hidden
                onChange={onPicked}
                aria-label="Photo or video files"
              />
              {/* eslint-disable-next-line no-restricted-syntax -- hidden file input (#394 carve-out): the camera capture behind the Upload affordance */}
              <input
                ref={cameraRef}
                type="file"
                accept="image/*"
                capture="environment"
                hidden
                onChange={onPicked}
                aria-label="Camera capture"
              />
              {/* eslint-disable-next-line no-restricted-syntax -- hidden file input (#394 carve-out): the document picker behind the Upload affordance */}
              <input
                ref={documentRef}
                type="file"
                multiple
                hidden
                onChange={onPicked}
                aria-label="Document files"
              />
            </>
          )}
        </div>
      )}

      {/* per-file upload progress pills (#479) — click one to dismiss it */}
      {uploads.length > 0 && (
        <div className="flex shrink-0 flex-wrap gap-2 border-b border-edge px-3 py-2">
          {uploads.map((u) => (
            <button
              key={u.key}
              onClick={() => setUploads((prev) => prev.filter((it) => it.key !== u.key))}
              title={u.detail ?? u.name}
              className={cn(
                "flex items-center gap-1.5 rounded-full border border-edge bg-surface px-3 py-1 text-xs",
                u.status === "error" ? "text-danger" : "text-ink-dim",
              )}
            >
              {u.status === "sending" && <Loader2 size={12} className="animate-spin" />}
              {u.status === "done" && <Check size={12} className="text-ok" />}
              {u.status === "error" && <X size={12} />}
              <span className="max-w-40 truncate">{u.name}</span>
              {u.status === "error" && u.detail && (
                <span className="max-w-56 truncate">— {u.detail}</span>
              )}
            </button>
          ))}
        </div>
      )}

      {/* two-pane body */}
      <div className="grid min-h-0 flex-1 sm:grid-cols-[minmax(0,20rem)_1fr]">
        {/* list pane — hidden on phone once an item is open; accepts external file
            drops as uploads into the current directory when the source allows (#479) */}
        <div
          data-testid="browser-list-pane"
          className={cn(
            "min-h-0 overflow-y-auto border-edge sm:border-r",
            selected && "hidden sm:block",
            fileDragOver && "bg-accent-dim ring-1 ring-inset ring-accent",
          )}
          {...externalDropProps}
        >
          {data.items.length === 0 ? (
            <EmptyState quote={activeQuery ? "No matches." : "Nothing here yet."} />
          ) : (
            <ul key={currentPath} className="flex flex-col p-2">
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
                          const now = Date.now();
                          if (now - lastNavRef.current < 500) return;
                          lastNavRef.current = now;
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

      {/* The phone source menu (#479): pick what kind of thing first, Telegram-style —
          each option opens the matching native surface via its hidden input. */}
      {source.upload && (
        <Sheet open={sourceMenuOpen} onClose={() => setSourceMenuOpen(false)} title="Upload">
          <div className="flex flex-col gap-1">
            <Button
              variant="ghost"
              className="w-full justify-start"
              onClick={() => {
                setSourceMenuOpen(false);
                galleryRef.current?.click();
              }}
            >
              <Image size={16} /> Photo or video
            </Button>
            <Button
              variant="ghost"
              className="w-full justify-start"
              onClick={() => {
                setSourceMenuOpen(false);
                cameraRef.current?.click();
              }}
            >
              <Camera size={16} /> Camera
            </Button>
            <Button
              variant="ghost"
              className="w-full justify-start"
              onClick={() => {
                setSourceMenuOpen(false);
                documentRef.current?.click();
              }}
            >
              <FileText size={16} /> Document
            </Button>
          </div>
          <p className="mt-3 text-xs text-ink-faint">
            Files land in the current folder{currentPath ? ` — ${currentPath}` : ""}.
          </p>
        </Sheet>
      )}
    </div>
  );
}
