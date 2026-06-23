/** Settings — platform info, theme, connected accounts, and what this thing is. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, KeyRound, Link, Moon, Sun, Unlink, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { EpsilonMark } from "@/components/Logo";
import { MemorySection } from "@/components/MemorySection";
import { Button, Card, Dot, Spinner, cn } from "@/components/ui";
import { api } from "@/lib/api";
import type { ModuleSnapshot } from "@/lib/contracts";
import { usePrefs } from "@/stores/prefs";

/** Union of the OAuth API scopes every installed module declares for *provider* (#241). */
function oauthScopeUnion(modules: ModuleSnapshot[] | undefined, provider: string): string | undefined {
  const scopes = new Set<string>();
  for (const snap of modules ?? []) {
    for (const scope of snap.manifest.oauth_scopes?.[provider] ?? []) scopes.add(scope);
  }
  return scopes.size ? [...scopes].join(" ") : undefined;
}

const OAUTH_PROVIDERS: { id: string; label: string; description: string }[] = [
  {
    id: "google",
    label: "Google",
    description: "Grants Google modules (Calendar, Gmail, Drive, …) access to your account.",
  },
];

function OAuthCredentialsForm({
  providerId,
  onSaved,
}: {
  providerId: string;
  onSaved: () => void;
}) {
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const save = useMutation({
    mutationFn: () => api.oauthSetClient(providerId, clientId.trim(), clientSecret.trim()),
    onSuccess: onSaved,
  });

  return (
    <form
      className="mt-3 flex flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (clientId.trim() && clientSecret.trim()) save.mutate();
      }}
    >
      <div>
        <label className="mb-1 block text-xs text-ink-dim">Client ID</label>
        <input
          type="text"
          autoComplete="off"
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
          placeholder="paste the client ID"
          className="w-full rounded-(--radius-field) border border-line bg-surface px-3 py-1.5 text-sm text-ink placeholder:text-ink-faint focus:outline-none focus:ring-1 focus:ring-accent"
        />
      </div>
      <div>
        <label className="mb-1 block text-xs text-ink-dim">
          Client Secret
          <span className="ml-1 text-ink-faint">(write-only — never shown again)</span>
        </label>
        <input
          type="password"
          autoComplete="off"
          value={clientSecret}
          onChange={(e) => setClientSecret(e.target.value)}
          placeholder="paste the client secret"
          className="w-full rounded-(--radius-field) border border-line bg-surface px-3 py-1.5 text-sm text-ink placeholder:text-ink-faint focus:outline-none focus:ring-1 focus:ring-accent"
        />
      </div>
      {save.isError && (
        <p className="text-xs text-danger">{(save.error as Error).message}</p>
      )}
      <Button
        type="submit"
        variant="primary"
        busy={save.isPending}
        disabled={!clientId.trim() || !clientSecret.trim()}
      >
        Save credentials
      </Button>
    </form>
  );
}

function OAuthProviderRow({ providerId }: { providerId: string }) {
  const queryClient = useQueryClient();
  const [showCredForm, setShowCredForm] = useState(false);

  const clientStatus = useQuery({
    queryKey: ["oauth-client-status", providerId],
    queryFn: () => api.oauthClientStatus(providerId),
  });

  const status = useQuery({
    queryKey: ["oauth-status", providerId],
    queryFn: () => api.oauthStatus(providerId),
  });

  // Request every installed module's API scopes for this provider, so one connect grants
  // them all (the core unions them onto the default identity scopes and accumulates) (#241).
  const modules = useQuery({ queryKey: ["modules"], queryFn: api.modules });

  const connect = useMutation({
    mutationFn: () => api.oauthConnect(providerId, oauthScopeUnion(modules.data, providerId)),
    onSuccess: (data) => {
      window.location.href = data.auth_url;
    },
  });

  const disconnect = useMutation({
    mutationFn: () => api.oauthDisconnect(providerId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["oauth-status", providerId] }),
  });

  if (status.isLoading || clientStatus.isLoading) return <Spinner />;

  const connected = status.data?.connected ?? false;
  const credentialsConfigured = clientStatus.data?.configured ?? false;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-2.5">
          <Dot tone={connected ? "ok" : "dim"} />
          <div>
            <p className="text-sm text-ink">{connected ? "Connected" : "Not connected"}</p>
            {connected && status.data?.scope && (
              <p className="text-xs text-ink-faint">{status.data.scope}</p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            onClick={() => setShowCredForm((v) => !v)}
            className="gap-1.5 text-xs"
            title={credentialsConfigured ? "Client credentials configured — click to update" : "Add client credentials"}
          >
            <KeyRound size={13} />
            {credentialsConfigured ? "Update credentials" : "Add credentials"}
          </Button>
          {connected ? (
            <Button
              variant="danger"
              onClick={() => disconnect.mutate()}
              disabled={disconnect.isPending}
            >
              <Unlink size={13} />
              Disconnect
            </Button>
          ) : (
            <Button
              variant="outline"
              onClick={() => connect.mutate()}
              disabled={connect.isPending || !credentialsConfigured}
              title={!credentialsConfigured ? "Add client credentials first" : undefined}
              className="gap-1.5"
            >
              <Link size={13} />
              {connect.isPending ? "Redirecting…" : "Connect"}
            </Button>
          )}
        </div>
      </div>
      {!credentialsConfigured && !showCredForm && (
        <p className="text-xs text-ink-dim">
          Add your Google OAuth client credentials above before connecting.
        </p>
      )}
      {showCredForm && (
        <OAuthCredentialsForm
          providerId={providerId}
          onSaved={() => {
            setShowCredForm(false);
            void queryClient.invalidateQueries({ queryKey: ["oauth-client-status", providerId] });
          }}
        />
      )}
      {(connect.isError || disconnect.isError) && (
        <p className="text-xs text-danger">
          {((connect.error || disconnect.error) as Error).message}
        </p>
      )}
    </div>
  );
}

function OAuthNotice({ message, tone }: { message: string; tone: "ok" | "error" }) {
  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-(--radius-field) border px-3 py-2 text-sm",
        tone === "ok"
          ? "border-ok/40 bg-ok/5 text-ok"
          : "border-danger/40 bg-danger/5 text-danger",
      )}
    >
      {tone === "ok" ? <CheckCircle2 size={15} /> : <XCircle size={15} />}
      {message}
    </div>
  );
}

/** A short curated list for the picker; the field accepts any IANA name by free text. */
const COMMON_TIMEZONES = [
  "UTC",
  "Europe/Belgrade",
  "Europe/London",
  "Europe/Berlin",
  "Europe/Moscow",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "Asia/Almaty",
  "Asia/Dubai",
  "Asia/Kolkata",
  "Asia/Shanghai",
  "Asia/Tokyo",
  "Australia/Sydney",
];

/** The operator's timezone — used by the agent's `now` tool (ADR-0039). Exported for tests. */
export function TimezoneCard() {
  const queryClient = useQueryClient();
  const tz = useQuery({ queryKey: ["timezone"], queryFn: api.timezone });
  const calStatus = useQuery({
    queryKey: ["module-status", "calendar"],
    queryFn: () => api.moduleStatus("calendar"),
    retry: false,
  });
  const save = useMutation({
    mutationFn: (value: string) => api.setTimezone(value),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["timezone"] }),
  });

  const current = tz.data?.timezone ?? "";
  const calendarTz =
    typeof calStatus.data?.google_timezone === "string" ? calStatus.data.google_timezone : null;

  return (
    <Card>
      <h3 className="mb-2 font-serif text-base text-ink">Timezone</h3>
      <p className="mb-3 text-sm text-ink-dim">
        The assistant uses this to know your current local time when you mention dates or
        times — e.g. “add it at 19:00” lands at 19:00 here.
      </p>
      {tz.isLoading ? (
        <Spinner />
      ) : (
        <div className="flex flex-col gap-2">
          <input
            type="text"
            list="tz-options"
            key={current}
            defaultValue={current}
            placeholder="e.g. Europe/Belgrade"
            onBlur={(e) => {
              const v = e.currentTarget.value.trim();
              if (v && v !== current) save.mutate(v);
            }}
            className="w-full rounded-(--radius-field) border border-line bg-surface px-3 py-1.5 text-sm text-ink placeholder:text-ink-faint focus:outline-none focus:ring-1 focus:ring-accent"
          />
          <datalist id="tz-options">
            {COMMON_TIMEZONES.map((z) => (
              <option key={z} value={z} />
            ))}
          </datalist>
          {save.isError && <p className="text-xs text-danger">{(save.error as Error).message}</p>}
          {save.isSuccess && <p className="text-xs text-ok">Saved.</p>}
          {calendarTz && calendarTz !== current && (
            <p className="text-xs text-warn">
              Your Google Calendar is set to <span className="font-mono">{calendarTz}</span>,
              which differs.{" "}
              <button
                type="button"
                className="underline hover:text-ink"
                onClick={() => save.mutate(calendarTz)}
              >
                Use {calendarTz}
              </button>
            </p>
          )}
        </div>
      )}
    </Card>
  );
}

export function SettingsScreen() {
  const theme = usePrefs((s) => s.theme);
  const setTheme = usePrefs((s) => s.setTheme);
  const info = useQuery({ queryKey: ["info"], queryFn: api.info });
  const location = useLocation();
  const navigate = useNavigate();
  const [oauthNotice, setOauthNotice] = useState<{
    message: string;
    tone: "ok" | "error";
  } | null>(null);

  // Read the oauth_connected / oauth_error query params the backend sets after the
  // consent callback. The notice is set during render (adjust-state-on-change, gated
  // so it shows once per callback) rather than in an effect; the URL is cleared in an
  // effect, where navigation is a genuine side effect (not a setState).
  const params = new URLSearchParams(location.search);
  const oauthConnected = params.get("oauth_connected");
  const oauthParam = oauthConnected ?? params.get("oauth_error");
  const [oauthHandled, setOauthHandled] = useState(false);
  if (oauthParam && !oauthHandled) {
    setOauthHandled(true);
    setOauthNotice(
      oauthConnected
        ? {
            message: `${oauthConnected.charAt(0).toUpperCase() + oauthConnected.slice(1)} connected successfully.`,
            tone: "ok",
          }
        : { message: "Connection failed or was cancelled.", tone: "error" },
    );
  } else if (!oauthParam && oauthHandled) {
    // The URL has been cleared — re-arm so a later callback shows its notice too.
    setOauthHandled(false);
  }
  useEffect(() => {
    if (oauthParam) navigate("/settings", { replace: true });
  }, [oauthParam, navigate]);

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-2xl flex-col gap-4 px-4 py-5">
        <h1 className="font-serif text-xl text-ink">Settings</h1>

        {oauthNotice && (
          <OAuthNotice message={oauthNotice.message} tone={oauthNotice.tone} />
        )}

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
          <h3 className="mb-3 font-serif text-base text-ink">Connected accounts</h3>
          <p className="mb-4 text-sm text-ink-dim">
            Grant the assistant access to external services. Tokens are stored
            encrypted in the secrets vault — modules never handle credentials
            directly.
          </p>
          <div className="flex flex-col gap-4">
            {OAUTH_PROVIDERS.map(({ id, label, description }) => (
              <div key={id}>
                <div className="mb-2 flex items-center gap-2">
                  <span className="text-sm font-medium text-ink">{label}</span>
                </div>
                <p className="mb-3 text-xs text-ink-dim">{description}</p>
                <OAuthProviderRow providerId={id} />
              </div>
            ))}
          </div>
        </Card>

        <TimezoneCard />

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

        <MemorySection />

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
