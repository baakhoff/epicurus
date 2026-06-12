/** Settings — platform info, theme, and what this thing is. */
import { useQuery } from "@tanstack/react-query";
import { Moon, Sun } from "lucide-react";

import { EpsilonMark } from "@/components/Logo";
import { Button, Card, Spinner } from "@/components/ui";
import { api } from "@/lib/api";
import { usePrefs } from "@/stores/prefs";

export function SettingsScreen() {
  const theme = usePrefs((s) => s.theme);
  const setTheme = usePrefs((s) => s.setTheme);
  const info = useQuery({ queryKey: ["info"], queryFn: api.info });

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-2xl flex-col gap-4 px-4 py-5">
        <h1 className="font-serif text-xl text-ink">Settings</h1>

        <Card>
          <h3 className="mb-2 font-serif text-base text-ink">Appearance</h3>
          <div className="flex gap-2">
            <Button
              variant={theme === "dark" ? "primary" : "outline"}
              onClick={() => setTheme("dark")}
            >
              <Moon size={15} />
              Lamplight dark
            </Button>
            <Button
              variant={theme === "light" ? "primary" : "outline"}
              onClick={() => setTheme("light")}
            >
              <Sun size={15} />
              Parchment light
            </Button>
          </div>
        </Card>

        <Card>
          <h3 className="mb-2 font-serif text-base text-ink">Platform</h3>
          {info.isLoading && <Spinner />}
          {info.data && (
            <dl className="grid grid-cols-2 gap-y-1.5 text-sm">
              <dt className="text-ink-dim">core version</dt>
              <dd className="font-mono text-ink">{info.data.core_version}</dd>
              <dt className="text-ink-dim">contract</dt>
              <dd className="font-mono text-ink">{info.data.contract_version}</dd>
              <dt className="text-ink-dim">tenant</dt>
              <dd className="font-mono text-ink">{info.data.tenant}</dd>
            </dl>
          )}
          {info.isError && <p className="text-sm text-warn">core unreachable</p>}
        </Card>

        <Card>
          <div className="flex items-start gap-3">
            <EpsilonMark size={34} />
            <div>
              <h3 className="font-serif text-base text-ink">epicurus</h3>
              <p className="mt-1 text-sm leading-relaxed text-ink-dim">
                A self-hosted, local-first assistant. Your models, your memory, your
                machine — modules extend it, and nothing leaves the garden unless you
                ask it to. Every asset in this app ships with it; it phones no one.
              </p>
              <p className="mt-2 text-xs text-ink-faint">
                Typefaces: Inter, Literata, JetBrains Mono (OFL, vendored). Icons:
                Lucide (ISC).
              </p>
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
}
