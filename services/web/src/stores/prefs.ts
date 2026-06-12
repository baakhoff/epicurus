/** UI preferences — persisted locally; the server never needs them. */
import { create } from "zustand";
import { persist } from "zustand/middleware";

export type Theme = "dark" | "light";

interface Prefs {
  theme: Theme;
  /** Model for new turns; null = the core's default. */
  model: string | null;
  /** Recently used hosted model ids (for quick re-pick). */
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
        if (model?.includes("/")) recents.unshift(model);
        set({ model, recentModels: recents.slice(0, 5) });
      },
    }),
    { name: "epicurus-prefs" },
  ),
);
