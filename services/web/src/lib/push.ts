/**
 * Web push subscribe/unsubscribe (#670, ADR-0102) — the browser-side half. Feature-detects
 * throughout (`PushManager` is absent on iOS Safari below 16.4 outside an installed PWA, and
 * in some other constrained contexts — see docs/services/web.md); every export degrades to
 * `false`/`null` rather than throwing, so a caller never needs its own try/catch just to
 * handle "this browser doesn't support push."
 */
import { api } from "@/lib/api";

export function isPushSupported(): boolean {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

/** Base64url (RFC 4648 §5) -> raw bytes — the shape `PushManager.subscribe` needs for
 *  `applicationServerKey` (the API hands back a base64url *string*, not the bytes it wants).
 *  Built via `new Uint8Array(n)` + a fill loop, not `Uint8Array.from` — the latter types as
 *  `Uint8Array<ArrayBufferLike>` (which includes `SharedArrayBuffer`), which the DOM's
 *  `BufferSource` (`ArrayBufferView<ArrayBuffer>`) rejects; the constructor form always owns
 *  a fresh concrete `ArrayBuffer`. */
function urlBase64ToUint8Array(base64url: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64url.length % 4)) % 4);
  const base64 = (base64url + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

/** This device's current subscription, or `null` if unsupported/unsubscribed. */
export async function getExistingSubscription(): Promise<PushSubscription | null> {
  if (!isPushSupported()) return null;
  const registration = await navigator.serviceWorker.ready;
  return registration.pushManager.getSubscription();
}

/**
 * Request permission (if not already granted) and subscribe this device. Returns the raw
 * `PushSubscription` on success, `null` on denial or an unsupported browser — the caller
 * still has to POST it to the backend (kept separate so the two independent failure points,
 * permission denial vs. the backend call, get their own error handling).
 */
export async function subscribeThisDevice(): Promise<PushSubscription | null> {
  if (!isPushSupported()) return null;
  const permission = await Notification.requestPermission();
  if (permission !== "granted") return null;
  const registration = await navigator.serviceWorker.ready;
  const { public_key } = await api.pushVapidPublicKey();
  return registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(public_key),
  });
}

/** Unsubscribe this device at the browser level. The matching backend row is left for the
 *  send path's existing prune-on-Gone (404/410) cleanup rather than deleted here — the
 *  browser's `PushSubscription` doesn't carry the backend's opaque row id, only the
 *  endpoint, so an immediate delete would need a second lookup for a purely cosmetic gain
 *  (the row lingers, unused, until the next failed send prunes it). */
export async function unsubscribeThisDevice(): Promise<boolean> {
  const subscription = await getExistingSubscription();
  if (!subscription) return true;
  return subscription.unsubscribe();
}

/** A human-readable guess at "this browser" for the device-label field ("Chrome on
 *  Windows") — best-effort labeling, not device fingerprinting; the operator can rename
 *  nothing today, but a sane default beats an empty label in the subscribed-devices list. */
export function guessDeviceLabel(): string {
  const ua = navigator.userAgent;
  const platform = /android/i.test(ua)
    ? "Android"
    : /iphone|ipad|ipod/i.test(ua)
      ? "iOS"
      : /mac os/i.test(ua)
        ? "Mac"
        : /windows/i.test(ua)
          ? "Windows"
          : /linux/i.test(ua)
            ? "Linux"
            : "device";
  const browser = /edg\//i.test(ua)
    ? "Edge"
    : /chrome/i.test(ua)
      ? "Chrome"
      : /firefox/i.test(ua)
        ? "Firefox"
        : /safari/i.test(ua)
          ? "Safari"
          : "Browser";
  return `${browser} on ${platform}`;
}
