import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { BrowserRouter, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { useRegisterSW } from "virtual:pwa-register/react";

import { SURFACES, modulePageNavs } from "@/app/registry";
import { EpsilonMark, Wordmark } from "@/components/Logo";
import { PowerOrb } from "@/components/PowerOrb";
import { PanelHost } from "@/components/Panel";
import { Button, cn } from "@/components/ui";
import { api } from "@/lib/api";
import { moduleIcon } from "@/lib/icons";
import { useDownloads } from "@/stores/downloads";
import { usePrefs } from "@/stores/prefs";
import { ChatScreen } from "@/screens/ChatScreen";
import { MemoryScreen } from "@/screens/MemoryScreen";
import { ModelsScreen } from "@/screens/ModelsScreen";
import { ModulePageScreen } from "@/screens/ModulePageScreen";
import { ModulesScreen } from "@/screens/ModulesScreen";
import { ObservabilityScreen } from "@/screens/ObservabilityScreen";
import { SettingsScreen } from "@/screens/SettingsScreen";

/** Shared NavLink class logic so core surfaces + module pages render identically. */
const railLinkClass = ({ isActive }: { isActive: boolean }) =>
  cn(
    "flex items-center gap-3 rounded-(--radius-field) px-3 py-2 text-sm transition-colors",
    isActive ? "bg-accent-dim text-accent-strong" : "text-ink-dim hover:bg-surface-2 hover:text-ink",
  );

const tabLinkClass = ({ isActive }: { isActive: boolean }) =>
  cn(
    "flex min-w-16 flex-1 flex-col items-center gap-0.5 py-2 text-[10px]",
    isActive ? "text-accent-strong" : "text-ink-faint",
  );

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, staleTime: 5_000, refetchOnWindowFocus: false },
  },
});

function UpdateToast() {
  const {
    needRefresh: [needRefresh],
    updateServiceWorker,
  } = useRegisterSW();
  if (!needRefresh) return null;
  return (
    <div className="fixed inset-x-4 bottom-20 z-50 sm:bottom-6 sm:left-auto sm:right-6 sm:w-80">
      <div className="flex items-center justify-between gap-3 rounded-(--radius-card) border border-edge bg-surface p-3 shadow-(--ep-shadow)">
        <p className="text-sm text-ink-dim">A new epicurus is ready.</p>
        <Button variant="primary" onClick={() => updateServiceWorker(true)}>
          Refresh
        </Button>
      </div>
    </div>
  );
}

function DownloadTray() {
  const active = useDownloads((s) => s.active);
  const dismiss = useDownloads((s) => s.dismiss);
  const location = useLocation();
  const pulls = Object.values(active);
  // The Models screen renders its own detailed progress.
  if (pulls.length === 0 || location.pathname === "/models") return null;
  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-20 z-40 flex justify-center sm:bottom-6">
      {pulls.map((pull) => {
        const pct =
          pull.total && pull.completed != null
            ? Math.round((pull.completed / pull.total) * 100)
            : null;
        return (
          <button
            key={pull.model}
            onClick={() => pull.done && dismiss(pull.model)}
            className="pointer-events-auto flex items-center gap-2 rounded-full border border-edge bg-surface px-4 py-1.5 text-xs text-ink-dim shadow-(--ep-shadow)"
          >
            <span className={cn("size-2 rounded-full", pull.error ? "bg-danger" : "bg-accent", !pull.done && "ep-breathe")} />
            {pull.model}
            {pull.error ? " — failed" : pull.done ? " — ready" : pct != null ? ` — ${pct}%` : "…"}
          </button>
        );
      })}
    </div>
  );
}

export function Shell() {
  // Module-contributed pages join the nav at runtime (ADR-0018): the shell renders
  // them, the modules only declare which archetype + supply data.
  const modules = useQuery({ queryKey: ["modules"], queryFn: api.modules, staleTime: 30_000 });
  const modulePages = modulePageNavs(modules.data ?? []);

  return (
    // `overflow-hidden` pins the fixed-height shell so the page body never scrolls —
    // every region below owns its own scroll. Without it, any child taller than the
    // viewport (notably the rail) leaks its overflow to <body>, and scrolling while
    // hovering that child drags the whole interface.
    <div className="flex h-dvh flex-col overflow-hidden sm:flex-row">
      {/* side rail (wide screens) — scrolls itself once the links outgrow the viewport */}
      <nav
        aria-label="Primary"
        className="hidden w-52 flex-col gap-1 overflow-y-auto border-r border-edge p-3 pt-safe sm:flex"
      >
        <div className="mb-4 flex items-center gap-2.5 px-2 pt-2">
          <EpsilonMark draw />
          <Wordmark />
        </div>
        {SURFACES.map(({ path, label, icon: Icon }) => (
          <NavLink key={path} to={path} end={path === "/"} className={railLinkClass}>
            <Icon size={17} />
            {label}
          </NavLink>
        ))}
        {modulePages.length > 0 && (
          <>
            <div className="my-2 border-t border-edge" />
            {modulePages.map(({ path, label, icon }) => {
              const Icon = moduleIcon(icon);
              return (
                <NavLink key={path} to={path} className={railLinkClass}>
                  <Icon size={17} />
                  {label}
                </NavLink>
              );
            })}
          </>
        )}
        <div className="mt-auto px-2 pb-1">
          <PowerOrb />
        </div>
      </nav>

      {/* main column */}
      <div className="flex min-h-0 flex-1 flex-col">
        {/* top bar (phones) */}
        <header className="flex items-center justify-between border-b border-edge px-4 py-2.5 pt-safe sm:hidden">
          <div className="flex items-center gap-2.5">
            <EpsilonMark draw />
            <Wordmark />
          </div>
          <PowerOrb />
        </header>

        <main className="min-h-0 flex-1">
          <Routes>
            <Route path="/" element={<ChatScreen />} />
            <Route path="/memory" element={<MemoryScreen />} />
            <Route path="/models" element={<ModelsScreen />} />
            <Route path="/modules" element={<ModulesScreen />} />
            <Route path="/settings" element={<SettingsScreen />} />
            <Route path="/observability" element={<ObservabilityScreen />} />
            <Route path="/m/:moduleName/:pageId" element={<ModulePageScreen />} />
          </Routes>
        </main>

        {/* bottom tab bar (phones) */}
        <nav className="flex overflow-x-auto border-t border-edge pb-safe sm:hidden">
          {SURFACES.map(({ path, label, icon: Icon }) => (
            <NavLink key={path} to={path} end={path === "/"} className={tabLinkClass}>
              <Icon size={20} />
              {label}
            </NavLink>
          ))}
          {modulePages.map(({ path, label, icon }) => {
            const Icon = moduleIcon(icon);
            return (
              <NavLink key={path} to={path} className={tabLinkClass}>
                <Icon size={20} />
                {label}
              </NavLink>
            );
          })}
        </nav>
      </div>

      <PanelHost />

      <DownloadTray />
      <UpdateToast />
    </div>
  );
}

export default function App() {
  const theme = usePrefs((s) => s.theme);

  // Keep the live theme in sync; the initial value is applied in main.tsx before
  // first paint so there is no flash and the shell can render immediately.
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Shell />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
