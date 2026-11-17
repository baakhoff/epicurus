/**
 * Permission decisions for web content — deny by default.
 *
 * Only the server's own origin is ever granted anything, and only:
 *
 * - `clipboard-sanitized-write` — the web UI's copy buttons (`navigator.clipboard.writeText`
 *   in `services/web/src/lib/clipboard.ts`).
 * - `fullscreen` — the HTML fullscreen API (a video or image a module shows full screen); it
 *   is the window going full screen, nothing more.
 *
 * Everything else is refused, notably `notifications` (web push cannot work in the app — no
 * push service — so the web UI hides its push controls when it runs here; native notifications
 * are a follow-up), `media` (no part of the UI records audio or video), geolocation, MIDI,
 * HID/serial/USB, `clipboard-read`, `openExternal` from the page, and every permission asked
 * by an identity provider's page or any other origin.
 */

export const GRANTED_ON_SERVER: ReadonlySet<string> = new Set([
  "clipboard-sanitized-write",
  "fullscreen",
]);

function originOf(url: string): string | null {
  try {
    const origin = new URL(url).origin;
    return origin === "null" ? null : origin;
  } catch {
    return null;
  }
}

/**
 * Whether `permission`, asked for by a page at `requestingUrl` (a URL or an origin — Electron
 * passes either depending on the handler), is granted while the server is `serverOrigin`.
 */
export function decidePermission(
  permission: string,
  requestingUrl: string,
  serverOrigin: string | null,
): boolean {
  if (serverOrigin === null) return false;
  if (!GRANTED_ON_SERVER.has(permission)) return false;
  return originOf(requestingUrl) === serverOrigin;
}
