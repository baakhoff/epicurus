/**
 * The command palette (#491) — the wayfinding capstone on #480: one keyboard-first
 * overlay over everything the shell already knows. Entries come from queries the shell
 * already holds — conversations (the ["sessions"] cache), core surfaces + module pages
 * (the same registry data the rail renders) — plus a few static actions. It is
 * deliberately NOT a second API surface: no new endpoints, no cross-content search.
 *
 * Keyboard contract: Ctrl/Cmd+K toggles it anywhere (the listener lives in the outer
 * component, mounted with the shell). Focus stays in the input the whole time — arrows
 * move the active option (combobox semantics via aria-activedescendant), Enter runs it,
 * Escape closes and useModalFocus (#487) hands focus back to wherever it was. Escape
 * listens in the capture phase, like Confirm, so a palette stacked over an open Sheet
 * closes alone. The dialog body is its own component so each open mounts fresh state —
 * no reset effects (react-hooks/set-state-in-effect).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  FilePlus,
  MessageCircle,
  Moon,
  Plus,
  Search,
  Sun,
  type LucideIcon,
} from "lucide-react";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { useNavigate } from "react-router-dom";

import { SURFACES, modulePageNavs } from "@/app/registry";
import { cn, useModalFocus } from "@/components/ui";
import { api } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { rankFiltered } from "@/lib/fuzzy";
import { moduleIcon } from "@/lib/icons";
import { useChat } from "@/stores/chat";
import { toast } from "@/stores/toasts";

/** The platform-correct hotkey label for affordances that advertise the palette. */
export function shortcutLabel(): string {
  const platform = navigator.platform ?? "";
  return /Mac|iP(hone|ad|od)/.test(platform) ? "⌘K" : "Ctrl K";
}

interface Entry {
  id: string;
  label: string;
  hint?: string;
  icon: LucideIcon;
  run: () => void;
}

const SESSION_LIMIT_BROWSING = 8;

export function CommandPalette({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  // The global hotkey — alive while closed too, so it can open the palette anywhere.
  // Toggle, so the same chord closes it (and never fires twice on key repeat).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey || e.repeat) return;
      if (e.key.toLowerCase() !== "k") return;
      e.preventDefault();
      onOpenChange(!open);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onOpenChange]);

  if (!open) return null;
  return <PaletteDialog onClose={() => onOpenChange(false)} />;
}

/** The dialog body — mounted only while open, so every open starts with fresh state. */
function PaletteDialog({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const openSession = useChat((s) => s.openSession);
  const newSession = useChat((s) => s.newSession);

  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const dialogRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // No autoFocus on the input: useModalFocus(initialFocus) focuses it AFTER capturing
  // the opener — an autoFocus child fires at commit, before the hook's effect, and the
  // Esc focus-restore would record the input itself as the opener.
  useModalFocus(dialogRef, true, inputRef);

  // Escape closes the palette alone — capture + stopPropagation is the Confirm-over-Sheet
  // stacking pattern (#487): an underlying Sheet's bubble-phase listener must not also fire.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      onClose();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  // All three are caches the shell already maintains (sessions sheet, rail, PowerOrb) —
  // mounting the dialog just refreshes them.
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: api.sessions });
  const modules = useQuery({
    queryKey: ["modules"],
    queryFn: () => api.modules(),
    staleTime: 30_000,
  });
  const power = useQuery({ queryKey: ["power"], queryFn: api.power });
  const paused = power.data?.state === "paused";

  const togglePower = useMutation({
    mutationFn: () => api.setPower(paused ? "idle" : "paused"),
    onSuccess: (status) => queryClient.setQueryData(["power"], status),
    onError: () => toast.error("Could not change the power state."),
  });

  const { sections, flat } = useMemo(() => {
    const modulePages = modulePageNavs(modules.data ?? []).filter((p) => p.archetype !== "review");
    // "New note" is honest only when a notes editor page exists to create into.
    const notesPage = modulePages.find((p) => p.module === "notes" && p.archetype === "editor");

    const actions: Entry[] = [
      {
        id: "action-new-chat",
        label: "New chat",
        icon: Plus,
        run: () => {
          newSession();
          navigate("/");
        },
      },
      // Gated on the power query having resolved: `paused` reads `false` before it has,
      // so a very fast open-and-click could otherwise send the wrong toggle.
      ...(power.data
        ? [
            {
              id: "action-power",
              label: paused ? "Wake up" : "Pause — unload models",
              icon: paused ? Sun : Moon,
              run: () => togglePower.mutate(),
            },
          ]
        : []),
      ...(notesPage
        ? [
            {
              id: "action-new-note",
              label: "New note",
              hint: "Notes",
              icon: FilePlus,
              run: () => navigate(`${notesPage.path}?new=1`),
            },
          ]
        : []),
    ];

    const sessionEntries: Entry[] = (sessions.data ?? [])
      .slice()
      .sort((a, b) => b.last_at.getTime() - a.last_at.getTime())
      .map((s) => ({
        id: `session-${s.id}`,
        label: s.title || "untitled",
        hint: relativeTime(s.last_at),
        icon: MessageCircle,
        run: () => {
          openSession(s.id);
          navigate("/");
        },
      }));

    const pageEntries: Entry[] = [
      ...SURFACES.map((s) => ({
        id: `page-${s.path}`,
        label: s.label,
        icon: s.icon,
        run: () => navigate(s.path),
      })),
      ...modulePages.map((p) => ({
        id: `page-${p.path}`,
        label: p.label,
        hint: p.module,
        icon: moduleIcon(p.icon),
        run: () => navigate(p.path),
      })),
    ];

    const needle = query.trim();
    const all: Array<{ title: string; entries: Entry[] }> = needle
      ? [
          { title: "Actions", entries: rankFiltered(actions, needle, (e) => e.label) },
          { title: "Conversations", entries: rankFiltered(sessionEntries, needle, (e) => e.label) },
          { title: "Pages", entries: rankFiltered(pageEntries, needle, (e) => e.label) },
        ]
      : [
          { title: "Actions", entries: actions },
          { title: "Conversations", entries: sessionEntries.slice(0, SESSION_LIMIT_BROWSING) },
          { title: "Pages", entries: pageEntries },
        ];

    let n = 0;
    const withStarts = all
      .filter((s) => s.entries.length > 0)
      .map((s) => {
        const start = n;
        n += s.entries.length;
        return { ...s, start };
      });
    return { sections: withStarts, flat: withStarts.flatMap((s) => s.entries) };
    // togglePower.mutate (not the mutation object) — react-query keeps `.mutate` stable
    // across renders, but returns a fresh mutation *object* each time, so depending on
    // `togglePower` itself would make this memo recompute on every render regardless of
    // the data it actually depends on.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- see above
  }, [
    modules.data,
    sessions.data,
    power.data,
    paused,
    query,
    navigate,
    newSession,
    openSession,
    togglePower.mutate,
  ]);

  // The keyboard cursor, clamped — a shrinking result list must never strand it.
  const cursor = Math.min(active, Math.max(flat.length - 1, 0));

  const run = (entry: Entry) => {
    entry.run();
    onClose();
  };

  const onInputKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive(Math.min(cursor + 1, Math.max(flat.length - 1, 0)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive(Math.max(cursor - 1, 0));
    } else if (e.key === "Home" && flat.length > 0) {
      e.preventDefault();
      setActive(0);
    } else if (e.key === "End" && flat.length > 0) {
      e.preventDefault();
      setActive(flat.length - 1);
    } else if (e.key === "Enter") {
      // A committed IME composition (CJK input) also dispatches Enter — that keystroke
      // commits the composed text, it must not also activate the highlighted entry.
      if (e.nativeEvent.isComposing) return;
      e.preventDefault();
      const entry = flat[cursor];
      if (entry) run(entry);
    }
  };

  // Keep the active option in view while arrowing (guarded: jsdom has no scrollIntoView).
  useEffect(() => {
    document.getElementById(`palette-option-${cursor}`)?.scrollIntoView?.({ block: "nearest" });
  }, [cursor]);

  return (
    <div
      ref={dialogRef}
      tabIndex={-1}
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
      className="fixed inset-0 z-50 outline-none"
    >
      <div className="absolute inset-0 bg-black/55" onClick={onClose} />
      <div className="absolute inset-x-3 top-[10dvh] mx-auto flex max-h-[70dvh] max-w-xl flex-col overflow-hidden rounded-(--radius-card) border border-edge bg-surface shadow-(--ep-shadow) sm:inset-x-6">
        <div className="flex items-center gap-2.5 border-b border-edge px-4 py-3">
          <Search size={16} className="shrink-0 text-ink-faint" />
          {/* eslint-disable-next-line no-restricted-syntax -- the palette combobox is
              chromeless by design: this row supplies the frame, and TextInput's bordered
              field styling can't be subtracted under plain cn() (no tailwind-merge). */}
          <input
            ref={inputRef}
            role="combobox"
            aria-expanded="true"
            aria-controls="palette-options"
            aria-activedescendant={flat.length > 0 ? `palette-option-${cursor}` : undefined}
            aria-autocomplete="list"
            aria-label="Search conversations, pages, and actions"
            placeholder="Search conversations, pages, actions…"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setActive(0);
            }}
            onKeyDown={onInputKeyDown}
            className="w-full bg-transparent text-sm text-ink placeholder:text-ink-faint focus:outline-none"
          />
          <kbd className="shrink-0 rounded border border-edge px-1.5 font-mono text-[10px] leading-4 text-ink-faint">
            esc
          </kbd>
        </div>
        <div
          id="palette-options"
          role="listbox"
          aria-label="Results"
          className="min-h-0 flex-1 overflow-y-auto p-2"
        >
          {flat.length === 0 && (
            <p className="px-3 py-6 text-center text-sm text-ink-dim">
              Nothing matches “{query.trim()}”.
            </p>
          )}
          {sections.map((section) => (
            <div key={section.title} role="group" aria-label={section.title}>
              <p
                aria-hidden="true"
                className="px-3 pb-1 pt-2 text-[11px] font-medium uppercase tracking-wide text-ink-faint"
              >
                {section.title}
              </p>
              {section.entries.map((entry, i) => {
                const flatIndex = section.start + i;
                const Icon = entry.icon;
                return (
                  <div
                    key={entry.id}
                    id={`palette-option-${flatIndex}`}
                    role="option"
                    aria-selected={flatIndex === cursor}
                    onClick={() => run(entry)}
                    onMouseMove={() => setActive(flatIndex)}
                    className={cn(
                      "flex cursor-pointer items-center gap-2.5 rounded-(--radius-field) px-3 py-2 text-sm",
                      flatIndex === cursor ? "bg-surface-2 text-ink" : "text-ink-dim",
                    )}
                  >
                    <Icon size={15} className="shrink-0 text-ink-faint" />
                    <span className="min-w-0 flex-1 truncate">{entry.label}</span>
                    {entry.hint && (
                      <span className="shrink-0 text-xs text-ink-faint">{entry.hint}</span>
                    )}
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
