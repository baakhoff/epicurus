/**
 * The Files screen — a core-owned file-space surface (ADR-0063). The Files browser
 * moved off the storage *module* onto core-owned endpoints (`/platform/v1/files/*`),
 * so it is now a first-party surface (like Models/Observability) rather than a module
 * page. It reuses the `browser` archetype's {@link BrowserView} as a component, backed
 * by a core source instead of the module page proxy.
 */
import { BrowserView, type BrowserSource } from "@/components/archetypes/BrowserView";
import { api } from "@/lib/api";

// A core-backed BrowserSource (ADR-0063). The view is data-source-agnostic; this adapter
// points it at the core file-space endpoints rather than a module's page proxy.
const filesSource: BrowserSource = {
  queryKey: ["files-page"],
  fetchPage: (path, q) => api.filesPage(path, q),
  readText: (p) => api.filesRead(p),
  move: (f, t) => api.filesMove(f, t),
};

export function FilesScreen() {
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-edge px-4 py-2.5">
        <h1 className="font-serif text-base text-ink">Files</h1>
      </div>
      <div className="min-h-0 flex-1">
        <BrowserView source={filesSource} />
      </div>
    </div>
  );
}
