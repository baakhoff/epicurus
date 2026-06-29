/** Chat bridges — connect external messaging channels (Discord, …) to the assistant (#369).
 *
 * Mirrors the connected-accounts pattern: a write-only bot-token field (stored core→OpenBao,
 * never in the browser), connect/disconnect, an on/off Switch, and live per-bridge status. The
 * data comes from the messaging module via the core; when that module isn't installed the whole
 * card hides itself (the bridges request 404s). */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Unlink } from "lucide-react";
import { useState } from "react";

import { Button, Card, Dot, Spinner, Switch, TextInput, Tooltip } from "@/components/ui";
import { api } from "@/lib/api";
import type { BridgeStatus } from "@/lib/contracts";

const BRIDGES_KEY = ["messaging-bridges"];

function BridgeTokenForm({ bridge, onSaved }: { bridge: string; onSaved: () => void }) {
  const [token, setToken] = useState("");
  const save = useMutation({
    mutationFn: () => api.connectBridge(bridge, token.trim()),
    onSuccess: onSaved,
  });

  return (
    <form
      className="mt-3 flex flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (token.trim()) save.mutate();
      }}
    >
      <div>
        <label className="mb-1 block text-xs text-ink-dim">
          Bot token
          <span className="ml-1 text-ink-faint">(write-only — stored in the vault, never shown)</span>
        </label>
        <TextInput
          type="password"
          autoComplete="off"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="paste the bot token"
        />
      </div>
      {save.isError && <p className="text-xs text-danger">{(save.error as Error).message}</p>}
      <Button type="submit" variant="primary" busy={save.isPending} disabled={!token.trim()}>
        Save &amp; connect
      </Button>
    </form>
  );
}

/** One bridge row: status + connect/disconnect + on/off. Exported for tests. */
export function BridgeRow({ bridge }: { bridge: BridgeStatus }) {
  const queryClient = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const refresh = () => queryClient.invalidateQueries({ queryKey: BRIDGES_KEY });

  const setEnabled = useMutation({
    mutationFn: (enabled: boolean) => api.setBridgeEnabled(bridge.bridge, enabled),
    onSuccess: refresh,
  });
  const disconnect = useMutation({
    mutationFn: () => api.disconnectBridge(bridge.bridge),
    onSuccess: () => {
      setShowForm(false);
      void refresh();
    },
  });

  // connected → green; configured-but-not-connected (connecting / disabled / error) → accent;
  // nothing stored → dim.
  const tone = bridge.connected ? "ok" : bridge.configured ? "accent" : "dim";
  const headline = bridge.connected
    ? "Connected"
    : !bridge.configured
      ? "Not connected"
      : !bridge.enabled
        ? "Disabled"
        : "Connecting…";

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-2.5">
          <Dot tone={tone} />
          <div>
            <p className="text-sm text-ink">{headline}</p>
            {bridge.detail && (
              <p className="text-xs text-ink-faint">{bridge.detail}</p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {bridge.configured && (
            <Switch
              checked={bridge.enabled}
              onChange={(next) => setEnabled.mutate(next)}
              disabled={setEnabled.isPending}
              label={`Toggle ${bridge.label} bridge`}
            />
          )}
          {bridge.configured ? (
            <>
              {/* Icon-only so the row never overflows a phone (#393): label → aria + tooltip. */}
              <Tooltip label="Update token">
                <Button
                  variant="ghost"
                  onClick={() => setShowForm((v) => !v)}
                  className="px-2.5"
                  aria-label="Update token"
                >
                  <KeyRound size={15} />
                </Button>
              </Tooltip>
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
            </>
          ) : (
            <Button variant="outline" onClick={() => setShowForm((v) => !v)} className="gap-1.5">
              <KeyRound size={13} />
              Connect
            </Button>
          )}
        </div>
      </div>
      {showForm && (
        <BridgeTokenForm
          bridge={bridge.bridge}
          onSaved={() => {
            setShowForm(false);
            void refresh();
          }}
        />
      )}
      {(setEnabled.isError || disconnect.isError) && (
        <p className="text-xs text-danger">
          {((setEnabled.error || disconnect.error) as Error).message}
        </p>
      )}
    </div>
  );
}

/** The Settings card listing every connectable bridge (#369). Hides itself when the messaging
 *  module isn't installed (the bridges request 404s). Exported for use in SettingsScreen. */
export function ChatBridgesCard() {
  const bridges = useQuery({
    queryKey: BRIDGES_KEY,
    queryFn: api.messagingBridges,
    retry: false,
  });

  // Messaging not installed / unreachable → no surface at all.
  if (bridges.isError) return null;

  // The in-process loopback bridge isn't operator-managed — show only connectable ones.
  const manageable = (bridges.data ?? []).filter((b) => b.manageable);

  return (
    <Card>
      <h3 className="mb-3 font-serif text-base text-ink">Chat bridges</h3>
      <p className="mb-4 text-sm text-ink-dim">
        Connect external messaging channels so you can talk to the assistant from them. Bot
        tokens are stored encrypted in the secrets vault — the module fetches them, the browser
        never holds a token.
      </p>
      {bridges.isLoading ? (
        <Spinner />
      ) : manageable.length === 0 ? (
        <p className="text-xs text-ink-dim">No bridges available.</p>
      ) : (
        <div className="flex flex-col gap-4">
          {manageable.map((b) => (
            <div key={b.bridge}>
              <div className="mb-2 flex items-center gap-2">
                <span className="text-sm font-medium text-ink">{b.label}</span>
              </div>
              <BridgeRow bridge={b} />
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
