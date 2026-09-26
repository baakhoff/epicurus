/**
 * Sign-in helpers the web shell shares (#969) — the pure half of the auth gate.
 *
 * The core is the OpenID Connect relying party: it runs the redirect dance, owns the session
 * cookie, and refuses a proxied `/platform/` request without a session with **401**
 * `{"detail": "Sign in to continue.", "code": "unauthenticated"}`. The shell's job is only to
 * notice it is signed out, say so, and send the browser to `GET /platform/v1/auth/login` with a
 * `next` that brings it back to where it was. Everything here is either pure or a thin seam
 * over a browser global, so the gate (`src/stores/auth.ts`) stays testable.
 *
 * **A leaf module on purpose.** `epFetch` (`src/lib/http.ts`) reports every 401 through
 * {@link reportUnauthenticated} below, and the auth store is what listens — so this file
 * imports nothing from the transport or the stores, and neither import direction can form a
 * cycle.
 */

/** The query parameter the core's callback puts failures in: `/?auth_error=<code>`. */
export const AUTH_ERROR_PARAM = "auth_error";

export const AUTH_SESSION_PATH = "/platform/v1/auth/session";
export const AUTH_LOGIN_PATH = "/platform/v1/auth/login";
export const AUTH_LOGOUT_PATH = "/platform/v1/auth/logout";

/** The core's own sign-in routes. They never answer 401 (they are exempt from enforcement),
 *  so a 401 on one of them must not be read as "the session ended". */
const AUTH_ROUTES_PREFIX = "/platform/v1/auth/";

/** How long an automatic redirect to the provider blocks the next one. A browser that comes
 *  back signed out this soon after being sent away did not complete sign-in — the provider
 *  refused silently, the user pressed Back, or the session cookie never stuck — and bouncing it
 *  straight back out would loop forever. Past the window, auto-redirect is honoured again. */
export const AUTO_REDIRECT_GUARD_MS = 30_000;

// Tab-scoped on purpose (sessionStorage): both marks describe *this* tab's recent history, and
// both survive the round trip to the provider and back, which is a same-tab navigation.
const AUTO_REDIRECT_AT_KEY = "epicurus-auth-auto-redirect-at";
const SIGNED_OUT_KEY = "epicurus-auth-signed-out";

/* ── The 401 signal ─────────────────────────────────────────────────────── */

let unauthenticatedHandler: (() => void) | null = null;

/** Install the one listener every transport's 401 reaches (the auth store does, once). */
export function setUnauthenticatedHandler(handler: (() => void) | null): void {
  unauthenticatedHandler = handler;
}

/** A `/platform/` request came back 401 — the core has no session for this browser. Called
 *  by `epFetch` and by the one XHR in the app (the archive upload), never by a screen. */
export function reportUnauthenticated(): void {
  unauthenticatedHandler?.();
}

/** Whether a 401 on `path` means "not signed in": a platform route, and not one of the sign-in
 *  routes themselves. */
export function isSignInEnforcedPath(path: string): boolean {
  return path.startsWith("/platform/") && !path.startsWith(AUTH_ROUTES_PREFIX);
}

/** An error the transport raised for a 401 — `ApiError` and the SSE readers' errors both carry
 *  the HTTP status as `status`. Structural, so this module stays free of `lib/api`. */
export function isUnauthenticatedError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { status?: unknown }).status === 401
  );
}

/** TanStack Query's retry policy: the house default (one retry), except never for a 401 — a
 *  second request with the same missing session can only be refused again, and the gate has
 *  already replaced the app with the sign-in screen by the time it would fire. */
export function retryUnlessSignedOut(failureCount: number, error: unknown): boolean {
  return !isUnauthenticatedError(error) && failureCount < 1;
}

/* ── Errors from the callback ───────────────────────────────────────────── */

/** Every failure code the core's callback can send back. */
export const AUTH_ERROR_CODES = [
  "provider_unreachable",
  "provider_error",
  "access_denied",
  "state_mismatch",
  "token_exchange_failed",
  "invalid_token",
  "not_allowed",
  "groups_claim_missing",
  "email_unverified",
  "misconfigured",
] as const;

/** A known code, or `"unknown"` for anything else — the raw value never goes further. */
export type AuthErrorCode = (typeof AUTH_ERROR_CODES)[number] | "unknown";

const AUTH_ERROR_MESSAGES: Record<AuthErrorCode, string> = {
  provider_unreachable:
    "The sign-in provider couldn't be reached. Try again in a moment — if it keeps happening, whoever runs this epicurus should check that the provider is up and reachable from it.",
  provider_error:
    "The sign-in provider reported a problem and didn't sign you in. Try again.",
  access_denied: "Sign-in was cancelled or refused at the provider.",
  state_mismatch:
    "That sign-in attempt expired or was started somewhere else. Start again from here.",
  token_exchange_failed:
    "epicurus couldn't finish signing you in with the provider. Try again — if it keeps happening, whoever runs this epicurus should check its client ID and secret.",
  invalid_token:
    "The provider's answer couldn't be verified, so you weren't signed in. Try again — if it keeps happening, whoever runs this epicurus should check its sign-in settings.",
  not_allowed:
    "Your account isn't allowed to use this epicurus. Ask whoever runs it to add you.",
  groups_claim_missing:
    "The provider didn't say which groups you belong to, so epicurus couldn't check your access. Whoever runs it needs to request the groups scope from the provider.",
  email_unverified:
    "Your email address isn't verified with the provider, so epicurus can't admit you by it. Verify it there, then try again.",
  misconfigured:
    "Sign-in isn't set up correctly on this epicurus. Ask whoever runs it to check its sign-in settings.",
  unknown:
    "Signing in didn't work. Try again — if it keeps happening, ask whoever runs this epicurus.",
};

/** Narrow a raw `auth_error` value to a code this shell has a sentence for. */
export function toAuthErrorCode(raw: string): AuthErrorCode {
  return (AUTH_ERROR_CODES as readonly string[]).includes(raw) ? (raw as AuthErrorCode) : "unknown";
}

/** The sentence the sign-in screen shows for a code. */
export function authErrorMessage(code: AuthErrorCode): string {
  return AUTH_ERROR_MESSAGES[code];
}

/**
 * Read `auth_error` off the address bar and remove it (a history *replace*, so Back does not
 * bring it back). Returns the narrowed code, or null when there was none. Called once, at boot,
 * before the router mounts — the router then reads the cleaned URL, and a reload cannot show a
 * stale failure.
 */
export function takeAuthErrorFromUrl(): AuthErrorCode | null {
  const url = new URL(window.location.href);
  if (!url.searchParams.has(AUTH_ERROR_PARAM)) return null;
  const raw = url.searchParams.get(AUTH_ERROR_PARAM) ?? "";
  url.searchParams.delete(AUTH_ERROR_PARAM);
  window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
  return toAuthErrorCode(raw);
}

/* ── Where to come back to ──────────────────────────────────────────────── */

/**
 * `path` if it is a same-origin app path, `/` otherwise. The core validates `next` too; this
 * keeps the shell from ever asking for something it would refuse — a protocol-relative
 * `//host`, a backslash trick, or a `/platform/` URL (the app never *lives* there; landing on
 * one after sign-in would show the API, not the app).
 */
export function safeNext(path: string): string {
  if (!path.startsWith("/") || path.startsWith("//") || path.startsWith("/\\")) return "/";
  if (path.startsWith("/platform/")) return "/";
  return path;
}

/** Where the user is now — path and query, minus any `auth_error` — as a `next` value. Read at
 *  the moment of leaving, so a 401 mid-session returns to the screen it interrupted, a share
 *  (`/?share=1`) to its composer, and a notification's deep link to its page. */
export function currentReturnPath(
  location: Pick<Location, "pathname" | "search"> = window.location,
): string {
  let search = location.search;
  if (search) {
    const params = new URLSearchParams(search);
    if (params.has(AUTH_ERROR_PARAM)) {
      params.delete(AUTH_ERROR_PARAM);
      const rest = params.toString();
      search = rest ? `?${rest}` : "";
    }
  }
  return safeNext(location.pathname + search);
}

/** The core's login route for a given return path. */
export function loginUrl(next: string): string {
  return `${AUTH_LOGIN_PATH}?next=${encodeURIComponent(safeNext(next))}`;
}

/** The full-page navigations sign-in needs, behind one object so tests can intercept them
 *  (jsdom implements no navigation). Always a *top-level* navigation: the provider's pages must
 *  own the window, and the core's callback must see its own cookies. */
export const authNav = {
  assign(url: string): void {
    window.location.assign(url);
  },
};

/* ── Tab-scoped marks ───────────────────────────────────────────────────── */

function readSession(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null; // storage disabled — behave as if nothing was ever marked
  }
}

function writeSession(key: string, value: string | null): void {
  try {
    if (value === null) window.sessionStorage.removeItem(key);
    else window.sessionStorage.setItem(key, value);
  } catch {
    /* storage disabled — the marks are an optimisation of the UX, never a correctness need */
  }
}

/** Whether an automatic redirect already left this tab within the guard window. */
export function autoRedirectedRecently(now: number = Date.now()): boolean {
  const at = Number(readSession(AUTO_REDIRECT_AT_KEY));
  return Number.isFinite(at) && at > 0 && now - at >= 0 && now - at < AUTO_REDIRECT_GUARD_MS;
}

/** Stamp an automatic redirect, so the next one within the guard window is held. */
export function markAutoRedirect(now: number = Date.now()): void {
  writeSession(AUTO_REDIRECT_AT_KEY, String(now));
}

/** The user signed out in this tab: auto-redirect stays off until they choose Sign in. */
export function markSignedOut(): void {
  writeSession(SIGNED_OUT_KEY, "1");
}

export function clearSignedOutMark(): void {
  writeSession(SIGNED_OUT_KEY, null);
}

export function signedOutHere(): boolean {
  return readSession(SIGNED_OUT_KEY) === "1";
}
