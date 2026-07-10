/** Settings — platform info, theme, connected accounts, and what this thing is. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, KeyRound, Link, Moon, RefreshCw, Sun, Unlink, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { ChatBridgesCard } from "@/components/ChatBridgesCard";
import { EpsilonMark } from "@/components/Logo";
import { MemorySection } from "@/components/MemorySection";
import {
  Button,
  Card,
  Dot,
  NumberInput,
  Spinner,
  TextArea,
  TextInput,
  Tooltip,
  cn,
} from "@/components/ui";
import { api, ApiError } from "@/lib/api";
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
        <TextInput
          type="text"
          autoComplete="off"
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
          placeholder="paste the client ID"
        />
      </div>
      <div>
        <label className="mb-1 block text-xs text-ink-dim">
          Client Secret
          <span className="ml-1 text-ink-faint">(write-only — never shown again)</span>
        </label>
        <TextInput
          type="password"
          autoComplete="off"
          value={clientSecret}
          onChange={(e) => setClientSecret(e.target.value)}
          placeholder="paste the client secret"
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

/** One connected-account row: status + credential/connect/disconnect actions. Exported for tests. */
export function OAuthProviderRow({ providerId }: { providerId: string }) {
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
  const modules = useQuery({ queryKey: ["modules"], queryFn: () => api.modules() });

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
  // Show a permission *count*, not the raw space-separated list of googleapis scope URLs (it
  // overflowed on mobile and was noise on desktop, #344). The full list stays on hover (title).
  const scopeCount = status.data?.scope
    ? status.data.scope.split(/\s+/).filter(Boolean).length
    : 0;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-2.5">
          <Dot tone={connected ? "ok" : "dim"} />
          <div>
            <p className="text-sm text-ink">{connected ? "Connected" : "Not connected"}</p>
            {connected && scopeCount > 0 && (
              <p className="text-xs text-ink-faint" title={status.data?.scope ?? undefined}>
                {scopeCount} permission{scopeCount === 1 ? "" : "s"} granted
              </p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {/* Icon-only so the row never overflows a phone (#393); the label moves to a
              tooltip + aria-label, matching the model-row treatment (#327/#384). */}
          <Tooltip label={credentialsConfigured ? "Update credentials" : "Add credentials"}>
            <Button
              variant="ghost"
              onClick={() => setShowCredForm((v) => !v)}
              className="px-2.5"
              aria-label={credentialsConfigured ? "Update credentials" : "Add credentials"}
            >
              <KeyRound size={15} />
            </Button>
          </Tooltip>
          {connected ? (
            <Tooltip label="Disconnect">
              <Button
                variant="danger"
                onClick={() => disconnect.mutate()}
                disabled={disconnect.isPending}
                className="px-2.5"
                aria-label="Disconnect"
              >
                <Unlink size={15} />
              </Button>
            </Tooltip>
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
          <TextInput
            type="text"
            list="tz-options"
            key={current}
            defaultValue={current}
            placeholder="e.g. Europe/Belgrade"
            onBlur={(e) => {
              const v = e.currentTarget.value.trim();
              if (v && v !== current) save.mutate(v);
            }}
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

/** Agent cycles — how many tool-calling rounds a turn runs before it must answer (#297). */
export function AgentCard() {
  const queryClient = useQueryClient();
  const prefs = useQuery({ queryKey: ["llmPrefs"], queryFn: api.llmPrefs });
  const save = useMutation({
    mutationFn: (value: number | null) => api.setAgentMaxSteps(value),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["llmPrefs"] }),
  });
  const current = prefs.data?.global_agent_max_steps ?? null;

  return (
    <Card>
      <h3 className="mb-2 font-serif text-base text-ink">Agent cycles</h3>
      <p className="mb-3 text-sm text-ink-dim">
        How many tool-calling rounds the assistant runs before it must answer. Higher lets it
        chain more steps (search → read → summarize) but a turn takes longer; lower keeps
        replies snappy. Leave blank for the default (4); the range is 1–12.
      </p>
      {prefs.isLoading ? (
        <Spinner />
      ) : (
        <div className="flex items-center gap-2">
          <NumberInput
            min={1}
            max={12}
            step={1}
            key={current ?? "default"}
            defaultValue={current ?? ""}
            placeholder="4"
            aria-label="Agent cycles"
            className="w-24"
            onBlur={(e) => {
              const raw = e.currentTarget.value.trim();
              const next = raw === "" ? null : Number(raw);
              if (next !== current) save.mutate(next);
            }}
          />
          {current !== null && (
            <Button variant="ghost" onClick={() => save.mutate(null)}>
              Reset to default
            </Button>
          )}
          {save.isError && <p className="text-xs text-danger">{(save.error as Error).message}</p>}
          {save.isSuccess && <p className="text-xs text-ok">Saved.</p>}
        </div>
      )}
    </Card>
  );
}

/**
 * Assistant instructions — the agent's base system prompt (#497, ADR-0083). It leads every turn
 * (identity, tone, tool-use), so it's server-stored and applies from any device; edits take
 * effect on the next message. "Reset to default" clears the override back to the shipped prompt.
 */
export function AssistantInstructionsCard() {
  const queryClient = useQueryClient();
  const prefs = useQuery({ queryKey: ["agentInstructions"], queryFn: api.agentInstructions });
  // `null` = following the saved/effective value; a string = an in-progress edit.
  const [draft, setDraft] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: (value: string | null) => api.setAgentInstructions(value),
    onSuccess: () => {
      setDraft(null); // fall back to following the freshly-saved value
      void queryClient.invalidateQueries({ queryKey: ["agentInstructions"] });
    },
  });

  const effective = prefs.data?.instructions ?? "";
  const isDefault = prefs.data?.is_default ?? true;
  const value = draft ?? effective;
  const dirty = draft !== null && draft.trim() !== effective.trim();
  // The prompt counts against every turn's context window and compaction never trims it, so warn
  // (don't block) past a generous size. ~4k chars is roughly 1k tokens.
  const SOFT_LIMIT = 4000;
  const overSoft = value.length > SOFT_LIMIT;

  // Warn before a browser-level navigation (reload, tab/app close) drops an unsaved draft. This is
  // the first long-form editor in Settings — unlike the instant-save cards around it, a half-written
  // system prompt is real work that shouldn't vanish on an accidental refresh (#536). (An in-app
  // route change goes through the declarative router, which `beforeunload` can't observe; blocking
  // those needs the data-router `useBlocker` — see the issue note.)
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = ""; // some browsers only show the native prompt when returnValue is set
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  return (
    <Card>
      <h3 className="mb-2 font-serif text-base text-ink">Assistant instructions</h3>
      <p className="mb-3 text-sm text-ink-dim">
        The base system prompt the assistant follows on every turn — its identity, tone, and how
        it uses tools. Edits take effect on your next message and apply from any device. Leave it
        on the default unless you want to steer its behaviour.
      </p>
      {prefs.isLoading ? (
        <Spinner />
      ) : (
        <div className="flex flex-col gap-2">
          <TextArea
            value={value}
            onChange={(e) => setDraft(e.target.value)}
            rows={10}
            aria-label="Assistant instructions"
            className="font-mono text-sm leading-relaxed"
            spellCheck={false}
          />
          <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-[11px]">
            <span className={cn(overSoft ? "text-warn" : "text-ink-faint")}>
              {value.length.toLocaleString()} characters
              {overSoft && " · long prompts eat into every turn's context and are never trimmed"}
            </span>
            {isDefault && !dirty && (
              <span className="text-ink-faint">Using the shipped default</span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="primary"
              busy={save.isPending}
              disabled={!dirty || !value.trim()}
              onClick={() => save.mutate(value)}
            >
              Save
            </Button>
            {(!isDefault || dirty) && (
              <Button variant="ghost" disabled={save.isPending} onClick={() => save.mutate(null)}>
                Reset to default
              </Button>
            )}
            {save.isError && (
              <p className="text-xs text-danger">{(save.error as Error).message}</p>
            )}
            {save.isSuccess && !dirty && <p className="text-xs text-ok">Saved.</p>}
          </div>
        </div>
      )}
    </Card>
  );
}

/**
 * Maintenance — run the core's background jobs as one coordinated batch (#383, ADR-0060).
 *
 * `POST /run` starts the batch server-side and returns immediately (#561): the card renders
 * live per-job progress from `current_run` and polls a few seconds apart while one is running
 * (the PowerOrb/ChatScreen poll pattern), and — since `current_run` comes from the same GET
 * this card already fetches on mount — a page refresh mid-batch rehydrates onto it for free.
 */
export function MaintenanceCard() {
  const qc = useQueryClient();
  const status = useQuery({
    queryKey: ["maintenance"],
    queryFn: api.maintenanceStatus,
    refetchInterval: (query) => (query.state.data?.current_run ? 3_000 : false),
  });
  const run = useMutation({
    mutationFn: () => api.runMaintenance(),
    // Whether this call started the batch (202) or found one already running (409, joined),
    // the GET is the source of truth afterward — refetch it either way.
    onSettled: () => qc.invalidateQueries({ queryKey: ["maintenance"] }),
  });

  const current = status.data?.current_run ?? null;
  const last = status.data?.last_run ?? null;
  const conflict = run.error instanceof ApiError && run.error.status === 409;
  const tone = (s: string): "ok" | "dim" | "accent" | "danger" =>
    s === "ok" ? "ok" : s === "running" ? "accent" : s === "error" ? "danger" : "dim";

  const runningJob = current?.jobs.find((j) => j.status === "running");
  const doneCount =
    current?.jobs.filter((j) => j.status !== "pending" && j.status !== "running").length ?? 0;

  return (
    <Card>
      <h3 className="mb-2 font-serif text-base text-ink">Maintenance</h3>
      <p className="mb-3 text-sm text-ink-dim">
        Run the background jobs — distil pending memories into facts and re-index modules — as one
        batch. Re-indexing rebuilds embeddings and can take a while; it runs in the background.
      </p>
      {status.isLoading ? (
        <Spinner />
      ) : (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              busy={run.isPending}
              disabled={current !== null}
              onClick={() => run.mutate()}
            >
              <RefreshCw size={14} />
              {current ? "Running…" : "Run maintenance now"}
            </Button>
            <span className="text-xs text-ink-faint">
              {status.data?.schedule_enabled
                ? `Scheduled nightly at ${String(status.data.schedule_hour).padStart(2, "0")}:00`
                : "Manual only — nightly schedule off"}
            </span>
          </div>
          {run.isError && !conflict && (
            <p className="text-sm text-danger">{(run.error as Error).message}</p>
          )}
          {current ? (
            <div className="text-[11px] text-ink-dim">
              {runningJob ? `${runningJob.label} — running` : "Starting"} · {doneCount}/
              {current.jobs.length} jobs
              <ul className="mt-1 flex flex-col gap-0.5">
                {current.jobs.map((j) => (
                  <li key={j.key} className="flex items-center gap-1.5">
                    <Dot tone={tone(j.status)} />
                    <span className="text-ink">{j.label}</span>
                    {j.detail && <span>· {j.detail}</span>}
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            last && (
              <div className="text-[11px] text-ink-dim">
                Last run{last.scope === "nightly" ? " (nightly)" : ""}:
                <ul className="mt-1 flex flex-col gap-0.5">
                  {last.jobs.map((j) => (
                    <li key={j.key} className="flex items-center gap-1.5">
                      <Dot tone={tone(j.status)} />
                      <span className="text-ink">{j.label}</span>
                      <span>· {j.detail}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )
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
  // Chat bridges are a messaging-module capability — hide the card (and skip its API
  // calls) unless messaging is installed and enabled (#430).
  const modules = useQuery({ queryKey: ["modules"], queryFn: () => api.modules() });
  const messagingEnabled =
    modules.data?.some((m) => m.manifest.name === "messaging" && m.enabled) ?? false;
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

        {messagingEnabled && <ChatBridgesCard />}

        <TimezoneCard />

        <AgentCard />

        <AssistantInstructionsCard />

        <MaintenanceCard />

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
