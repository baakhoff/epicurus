/**
 * The navigation policy of the main window — pure decisions, wired up in `window.ts`.
 *
 * - **Top-level navigation** to any `http:`/`https:` address is allowed: signing in
 *   (`/platform/v1/auth/login` → the identity provider → back) and connecting an account
 *   (`window.location.href = auth_url`) are full-page navigations away from the server and back.
 * - **Everything else** a page asks to navigate to — `file:`, `data:`, `javascript:`, the app's
 *   own `epicurus-app:` pages, custom schemes an identity provider might try — is refused.
 * - **New windows** (`target="_blank"`, `window.open`) never open inside the app: `http:`,
 *   `https:` and `mailto:` go to the system's default handler, anything else is dropped.
 * - When the window shows a page that is **not on the server's origin**, the host it is on is
 *   shown in a bar above the page, with a button back to the server.
 */

/** The scheme the app's own local pages (connect, offline, the host bar) are served from. */
export const APP_SCHEME = "epicurus-app";
export const APP_HOST = "local";
export const APP_ORIGIN = `${APP_SCHEME}://${APP_HOST}`;

export type NavigationDecision = "allow" | "refuse";
export type WindowOpenDecision = "open-external" | "refuse";

function parse(url: string): URL | null {
  try {
    return new URL(url);
  } catch {
    return null;
  }
}

/** A navigation the page itself started (a link, a form, `location.href = …`, a redirect). */
export function decideNavigation(target: string): NavigationDecision {
  const url = parse(target);
  if (!url) return "refuse";
  if (url.protocol !== "http:" && url.protocol !== "https:") return "refuse";
  if (url.username !== "" || url.password !== "") return "refuse";
  return "allow";
}

/** A request for a new window. The app never opens a second window for page content. */
export function decideWindowOpen(target: string): WindowOpenDecision {
  const url = parse(target);
  if (!url) return "refuse";
  if (url.protocol === "http:" || url.protocol === "https:" || url.protocol === "mailto:") {
    return "open-external";
  }
  return "refuse";
}

/** Whether `url` is one of the app's own local pages. */
export function isAppPage(url: string): boolean {
  const parsed = parse(url);
  return parsed !== null && parsed.protocol === `${APP_SCHEME}:` && parsed.host === APP_HOST;
}

/**
 * What the host bar shows for the page at `current`: `null` while the window is on the server
 * (or on one of the app's own pages), otherwise the host the user is actually looking at.
 */
export function offOriginHost(current: string, serverOrigin: string | null): string | null {
  const url = parse(current);
  if (!url || (url.protocol !== "http:" && url.protocol !== "https:")) return null;
  if (serverOrigin !== null && url.origin === serverOrigin) return null;
  return url.host;
}

/** Whether `url` is on the server's origin. */
export function isOnServer(url: string, serverOrigin: string | null): boolean {
  const parsed = parse(url);
  return serverOrigin !== null && parsed !== null && parsed.origin === serverOrigin;
}
