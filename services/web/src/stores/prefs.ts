/** UI preferences — persisted locally; the server never needs them. */
import { create } from "zustand";
import { persist } from "zustand/middleware";

import { isHostedModelId } from "@/lib/format";

export type Theme = "dark" | "light";

interface Prefs {
  theme: Theme;
  /** Model for new turns; null = the core's default. */
  model: string | null;
  /**
   * Recently used hosted model ids — a device-local warm cache for the picker. The server's
   * saved-models list (#496) is the source of truth; this just gives an instant, offline echo
   * of what was picked here before that query resolves.
   */
  recentModels: string[];
  setTheme: (theme: Theme) => void;
  setModel: (model: string | null) => void;
}

export const usePrefs = create<Prefs>()(
  persist(
    (set, get) => ({
      theme: "dark",
      model: null,
      recentModels: [],
      setTheme: (theme) => set({ theme }),
      setModel: (model) => {
        const recents = get().recentModels.filter((m) => m !== model);
        // Only a genuine hosted id joins the recents — a known `<provider>/…` prefix. This is
        // the client-side fix for the old `includes("/")` bug, which mis-filed a local
        // `hf.co/org/model:tag` as hosted (#496).
        if (model && isHostedModelId(model)) recents.unshift(model);
        set({ model, recentModels: recents.slice(0, 5) });
      },
    }),
    { name: "epicurus-prefs" },
  ),
);
