/** Web push (#670, ADR-0102; delivery-state surface #797) — subscribe this device, manage
 *  subscribed devices, per-category push toggles, quiet hours, and a delivery-state readout.
 *  Since #797 the notification center records everything unconditionally, so each category's
 *  toggle governs push delivery only (`center` is sent as `true` to converge stored prefs);
 *  the card also warns loudly when push is on but no device is registered — the
 *  misconfiguration that used to fail silently, per notification, forever. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, BellOff, Send, Trash2, TriangleAlert } from "lucide-react";
import { useEffect, useState } from "react";

import { Button, Card, EmptyState, Spinner, Switch, TextInput } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import type { PushDeviceRecord, PushStatus } from "@/lib/contracts";
import {
  getExistingSubscription,
  guessDeviceLabel,
  isPushSupported,
  subscribeThisDevice,
  unsubscribeThisDevice,
} from "@/lib/push";

const SUBSCRIPTIONS_KEY = ["push-subscriptions"];
const PREFS_KEY = ["push-prefs"];
const STATUS_KEY = ["push-status"];
// Shared with EventAlertsCard so both cards read/invalidate one cache entry.
const EVENT_SUBSCRIPTIONS_KEY = ["event-subscriptions"];

function formatWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/** Plain-language reading of one delivery attempt (#797) — `outcome: "sent"` only means the
 *  send path ran, so the counts decide whether it actually worked. */
function describeAttempt(attempt: NonNullable<PushStatus["last_attempt"]>): {
  text: string;
  failed: boolean;
} {
  if (attempt.outcome === "sent") {
    if (attempt.sent_count > 0) {
      const failures = attempt.failed_count > 0 ? `, ${attempt.failed_count} failed` : "";
      return { text: `delivered to ${attempt.sent_count} device(s)${failures}`, failed: false };
    }
    if (attempt.failed_count > 0) {
      return { text: `failed — 0 of ${attempt.failed_count} device(s) reached`, failed: true };
    }
    if (attempt.pruned_count > 0) {
      return { text: `no live devices — ${attempt.pruned_count} stale registration(s) removed`, failed: true };
    }
    return { text: "nothing was delivered", failed: true };
  }
  if (attempt.outcome === "queued") return { text: "held for the quiet-hours digest", failed: false };
  if (attempt.outcome === "skipped_rate_limited") return { text: "skipped — hourly rate cap reached", failed: false };
  if (attempt.outcome === "skipped_no_devices") return { text: "skipped — no registered devices", failed: true };
  return { text: attempt.outcome, failed: false };
}

/** The loud configure-time warning (#797): push is on somewhere, but there is no device to
 *  deliver to — previously discovered per-notification in server logs, or never. */
function NoDeviceWarning() {
  const status = useQuery({ queryKey: STATUS_KEY, queryFn: api.pushStatus });
  const prefs = useQuery({ queryKey: PREFS_KEY, queryFn: api.pushPrefs });
  const eventSubs = useQuery({ queryKey: EVENT_SUBSCRIPTIONS_KEY, queryFn: api.eventSubscriptions });

  if (!status.data || status.data.device_count > 0) return null;
  const categoryPushOn = prefs.data
    ? Object.values(prefs.data.categories).some((c) => c.push)
    : false;
  const eventPushOn = (eventSubs.data ?? []).some((s) => s.push);
  if (!categoryPushOn && !eventPushOn) return null;

  return (
    <div className="mb-4 flex items-start gap-2.5 rounded-(--radius-card) border border-warn/40 bg-warn/5 p-3">
      <TriangleAlert size={15} className="mt-0.5 shrink-0 text-warn" />
      <p className="text-sm text-warn">
        Push is turned on, but no device is registered — notifications will be recorded in
        the notification center and reach no device. Subscribe a device below to receive them.
      </p>
    </div>
  );
}

/** Last-attempt readout (#797): when a push was last tried and what happened to it, straight
 *  from `GET /push/status` — the answer that used to require reading server logs. */
function DeliveryState() {
  const status = useQuery({ queryKey: STATUS_KEY, queryFn: api.pushStatus });

  if (status.isLoading) return <Spinner />;
  if (status.isError || !status.data)
    return <p className="text-sm text-danger">Could not load delivery status.</p>;

  const attempt = status.data.last_attempt;
  if (!attempt) {
    return (
      <p className="text-xs text-ink-dim">
        No push attempted since the server last started. Send a test to check the pipeline.
      </p>
    );
  }
  const { text, failed } = describeAttempt(attempt);
  return (
    <p className={`text-xs ${failed ? "text-warn" : "text-ink-dim"}`}>
      Last push attempt {formatWhen(attempt.at)} ({attempt.category}): {text}.
    </p>
  );
}

/** Subscribe/unsubscribe control for the browser the operator is currently using. Its
 *  subscribed/not state is read straight from the Push API (not the backend list), since
 *  that's the only place that can answer "is *this* device subscribed" unambiguously. */
function ThisDeviceControl() {
  const qc = useQueryClient();
  // null = still checking; the Push API call is async even when the answer is "no".
  const [subscribed, setSubscribed] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getExistingSubscription().then((sub) => {
      if (!cancelled) setSubscribed(sub != null);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const refresh = () => qc.invalidateQueries({ queryKey: SUBSCRIPTIONS_KEY });

  const subscribe = useMutation({
    mutationFn: async () => {
      const sub = await subscribeThisDevice();
      if (!sub) throw new Error("Permission was denied, or this browser doesn't support push.");
      const json = sub.toJSON();
      if (!json.endpoint || !json.keys?.p256dh || !json.keys.auth) {
        throw new Error("The browser returned an incomplete subscription.");
      }
      await api.createPushSubscription({
        endpoint: json.endpoint,
        p256dh: json.keys.p256dh,
        auth: json.keys.auth,
        device_label: guessDeviceLabel(),
      });
    },
    onSuccess: () => {
      setSubscribed(true);
      void refresh();
    },
  });

  const unsubscribe = useMutation({
    mutationFn: () => unsubscribeThisDevice(),
    onSuccess: () => {
      setSubscribed(false);
      void refresh();
    },
  });

  const error = subscribe.error ?? unsubscribe.error;

  if (!isPushSupported()) {
    return (
      <p className="text-xs text-ink-dim">
        This browser doesn&apos;t support push notifications. On iOS, install this app to the
        home screen first (Share → Add to Home Screen, iOS 16.4+) — push only works from the
        installed app, not the Safari tab.
      </p>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          {subscribed ? (
            <Bell size={15} className="text-ok" />
          ) : (
            <BellOff size={15} className="text-ink-faint" />
          )}
          <p className="text-sm text-ink">
            {subscribed === null
              ? "Checking this device…"
              : subscribed
                ? "This device is subscribed"
                : "This device is not subscribed"}
          </p>
        </div>
        {subscribed === true && (
          <Button variant="outline" onClick={() => unsubscribe.mutate()} busy={unsubscribe.isPending}>
            Unsubscribe
          </Button>
        )}
        {subscribed === false && (
          <Button variant="primary" onClick={() => subscribe.mutate()} busy={subscribe.isPending}>
            Subscribe
          </Button>
        )}
      </div>
      {error && <p className="mt-2 text-xs text-danger">{error.message}</p>}
    </div>
  );
}

function DeviceRow({ device }: { device: PushDeviceRecord }) {
  const qc = useQueryClient();
  const remove = useMutation({
    mutationFn: () => api.deletePushSubscription(device.id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: SUBSCRIPTIONS_KEY }),
  });

  return (
    <li className="flex items-center justify-between gap-3 rounded-(--radius-card) border border-edge bg-surface-2 p-3">
      <div>
        <p className="text-sm text-ink">{device.device_label || "Unnamed device"}</p>
        <p className="text-xs text-ink-faint">
          Subscribed {formatWhen(device.created_at)}
          {device.last_seen_at && ` · last used ${formatWhen(device.last_seen_at)}`}
        </p>
      </div>
      <Button
        variant="ghost"
        onClick={() => remove.mutate()}
        disabled={remove.isPending}
        className="px-2"
        aria-label={`Remove ${device.device_label || "device"}`}
      >
        <Trash2 size={14} />
      </Button>
    </li>
  );
}

function CategoryToggles() {
  const qc = useQueryClient();
  const prefs = useQuery({ queryKey: PREFS_KEY, queryFn: api.pushPrefs });
  const setPrefs = useMutation({
    // `center` is always sent as true (#797): the notification center records everything
    // regardless, so writing true converges any stored pre-#797 value to the new truth.
    mutationFn: (vars: { id: string; push: boolean }) =>
      api.setPushPrefs({ categories: { [vars.id]: { push: vars.push, center: true } } }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: PREFS_KEY }),
  });

  if (prefs.isLoading) return <Spinner />;
  if (prefs.isError || !prefs.data) return <p className="text-sm text-danger">Could not load preferences.</p>;

  return (
    <div className="flex flex-col gap-2">
      {prefs.data.known_categories.map((cat) => {
        const current = prefs.data.categories[cat.id] ?? { push: true, center: true };
        return (
          <div key={cat.id} className="flex items-center justify-between gap-3">
            <span className="text-sm text-ink">{cat.label}</span>
            <Switch
              checked={current.push}
              onChange={(next) => setPrefs.mutate({ id: cat.id, push: next })}
              disabled={setPrefs.isPending}
              label={`Push notifications for ${cat.label}`}
            />
          </div>
        );
      })}
    </div>
  );
}

function QuietHoursEditor() {
  const qc = useQueryClient();
  const prefs = useQuery({ queryKey: PREFS_KEY, queryFn: api.pushPrefs });
  const [start, setStart] = useState("22:00");
  const [end, setEnd] = useState("07:00");
  const [loadedFrom, setLoadedFrom] = useState<string | null>(null);

  // Seed the local draft from the server once per fetch — an uncontrolled-to-controlled
  // draft, same shape as the maintenance schedule editor (interdependent fields, explicit
  // Save, so a stray blur mid-edit can't half-save a start without its end).
  const fetchedAt = prefs.data ? `${prefs.data.quiet_hours_start}|${prefs.data.quiet_hours_end}` : null;
  if (prefs.data && fetchedAt !== loadedFrom) {
    setLoadedFrom(fetchedAt);
    setStart(prefs.data.quiet_hours_start);
    setEnd(prefs.data.quiet_hours_end);
  }

  const setEnabled = useMutation({
    mutationFn: (enabled: boolean) => api.setPushPrefs({ quiet_hours_enabled: enabled }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: PREFS_KEY }),
  });
  const save = useMutation({
    mutationFn: () =>
      api.setPushPrefs({ quiet_hours_enabled: true, quiet_hours_start: start, quiet_hours_end: end }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: PREFS_KEY }),
  });

  if (prefs.isLoading) return <Spinner />;
  if (prefs.isError || !prefs.data) return <p className="text-sm text-danger">Could not load preferences.</p>;

  const dirty = start !== prefs.data.quiet_hours_start || end !== prefs.data.quiet_hours_end;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-sm text-ink">Quiet hours</p>
          <p className="text-xs text-ink-dim">
            Notifications during this window are held and delivered as one summary once it ends.
          </p>
        </div>
        <Switch
          checked={prefs.data.quiet_hours_enabled}
          onChange={(next) => setEnabled.mutate(next)}
          disabled={setEnabled.isPending}
          label="Enable quiet hours"
        />
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="mb-1 block text-xs text-ink-dim" htmlFor="quiet-hours-start">
            From
          </label>
          <TextInput
            id="quiet-hours-start"
            type="time"
            value={start}
            onChange={(e) => setStart(e.target.value)}
            className="w-32"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-ink-dim" htmlFor="quiet-hours-end">
            Until
          </label>
          <TextInput
            id="quiet-hours-end"
            type="time"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
            className="w-32"
          />
        </div>
        {dirty && (
          <Button variant="primary" onClick={() => save.mutate()} busy={save.isPending}>
            Save
          </Button>
        )}
      </div>
      {save.isError && (
        <p className="text-xs text-danger">
          {save.error instanceof ApiError ? save.error.detail : "Could not save."}
        </p>
      )}
    </div>
  );
}

function TestNotificationButton() {
  const qc = useQueryClient();
  const send = useMutation({
    mutationFn: () => api.sendTestPushNotification("system"),
    // The delivery-state readout should reflect this attempt immediately (#797).
    onSettled: () => void qc.invalidateQueries({ queryKey: STATUS_KEY }),
  });

  const resultLabel = (): string | null => {
    if (!send.data) return null;
    if (send.data.outcome === "sent") {
      if (send.data.sent_count === 0 && send.data.failed_count > 0) {
        return "Delivery failed on every device — see the delivery status above.";
      }
      return `Sent to ${send.data.sent_count} device(s).`;
    }
    if (send.data.outcome === "skipped_no_devices") return "No devices to send it to.";
    if (send.data.outcome === "skipped_disabled") return "The System category has push turned off.";
    if (send.data.outcome === "queued") return "Quiet hours are active — queued for the digest.";
    if (send.data.outcome === "skipped_rate_limited") return "Rate limit reached — try again later.";
    return send.data.outcome;
  };

  return (
    <div className="flex items-center gap-3">
      <Button variant="outline" onClick={() => send.mutate()} busy={send.isPending} className="gap-1.5">
        <Send size={13} />
        Send test notification
      </Button>
      {resultLabel() && <p className="text-xs text-ink-dim">{resultLabel()}</p>}
      {send.isError && <p className="text-xs text-danger">Could not send.</p>}
    </div>
  );
}

export function PushNotificationsCard() {
  const devices = useQuery({ queryKey: SUBSCRIPTIONS_KEY, queryFn: api.pushSubscriptions });

  return (
    <Card>
      <h3 className="mb-3 font-serif text-base text-ink">Push notifications</h3>
      <p className="mb-4 text-sm text-ink-dim">
        Get notified on this device, even when the app isn&apos;t open. Works on desktop
        Chrome/Edge and installed Android/iOS PWAs. Every notification is always recorded in
        the notification center — push is how it also reaches your devices.
      </p>

      <NoDeviceWarning />

      <ThisDeviceControl />

      <div className="my-4 border-t border-edge" />

      <h4 className="mb-2 text-xs font-medium tracking-wide text-ink-dim uppercase">
        Subscribed devices
      </h4>
      {devices.isLoading ? (
        <Spinner />
      ) : devices.isError ? (
        <p className="text-sm text-danger">Could not load subscribed devices.</p>
      ) : devices.data && devices.data.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {devices.data.map((d) => (
            <DeviceRow key={d.id} device={d} />
          ))}
        </ul>
      ) : (
        <EmptyState quote="No devices subscribed yet." />
      )}

      <div className="my-4 border-t border-edge" />

      <h4 className="mb-2 text-xs font-medium tracking-wide text-ink-dim uppercase">Categories</h4>
      <p className="mb-2 text-xs text-ink-dim">
        These switches control push delivery only — every category is always recorded in the
        notification center.
      </p>
      <CategoryToggles />

      <div className="my-4 border-t border-edge" />

      <QuietHoursEditor />

      <div className="my-4 border-t border-edge" />

      <h4 className="mb-2 text-xs font-medium tracking-wide text-ink-dim uppercase">Delivery</h4>
      <div className="flex flex-col gap-3">
        <DeliveryState />
        <TestNotificationButton />
      </div>
    </Card>
  );
}
