/**
 * The auth gate's state (#969): whether this browser may see the app and, when it may not,
 * what the sign-in screen should say.
 *
 * **Phases.** `idle` → `checking` (one `GET /platform/v1/auth/session` before the app's
 * data-fetching tree mounts, so a signed-out browser never fires a burst of refused requests)
 * → `app`, or `signed-out` (the sign-in screen), or `redirecting` (a full-page navigation to
 * the core's login route is under way). `app` is also where the gate lands whenever sign-in is
 * off — `mode: "none"`, the default everywhere — which is how an unconfigured deployment
 * behaves exactly as it did before sign-in existed.
 *
 * **An unanswered check opens the app, never the sign-in screen.** A core that is down or
 * restarting cannot say whether sign-in is on, and the shell has always stayed up in front of an
 * unreachable core (the connection banner explains it, #494). So the session check failing —
 * or simply not answering within {@link SESSION_WAIT_MS} — mounts the app exactly as before,
 * and correctness comes from the other direction: the first request the core *does* answer with
 * a 401 flips the gate. An older core with no auth routes (404) has no sign-in at all, which is
 * `mode: "none"` by definition.
 *
 * **A 401 is authoritative only when sign-in was known to be on.** Every `/platform/` 401 lands
 * in {@link AuthState.reportUnauthenticated} (via `epFetch`). With a last-known session in
 * `oidc` mode the app is replaced at once; with none known — or with `mode: "none"`, where a 401
 * can only come from something *in front of* epicurus, or from an operator who just turned
 * sign-in on — the gate asks the core again first, and moves only on its answer.
 *
 * None of this is security: the core enforces every request. The gate is what makes a refused
 * request look like a sign-in screen instead of a page of errors.
 */
import { create } from "zustand";

import { api, ApiError } from "@/lib/api";
import {
  authNav,
  autoRedirectedRecently,
  clearSignedOutMark,
  currentReturnPath,
  loginUrl,
  markAutoRedirect,
  markSignedOut,
  setUnauthenticatedHandler,
  signedOutHere,
  takeAuthErrorFromUrl,
  type AuthErrorCode,
} from "@/lib/auth";
import { signInRequired, type AuthSession } from "@/lib/contracts";

export type AuthPhase = "idle" | "checking" | "app" | "signed-out" | "redirecting";

/** Why the sign-in screen is up: the browser arrived without a session, a session it had
 *  ended mid-use (a 401), or the user signed out here. */
export type SignedOutReason = "arrived" | "expired" | "signed-out";

/** How long the boot check may hold the app back before the gate stops waiting and mounts it
 *  anyway. A LAN core answers in milliseconds; a box that is off the VPN can leave a request
 *  hanging for the OS's whole connect timeout, and the shell has never made anyone wait that
 *  out. The check keeps running — a late "signed out" still flips the gate. */
export const SESSION_WAIT_MS = 3_000;

/** What an older core's 404 means: it predates sign-in, so there is none. */
const NO_SIGN_IN: AuthSession = {
  mode: "none",
  signed_in: false,
  provider_name: null,
  auto_redirect: false,
  user: null,
  expires_at: null,
};

export interface AuthState {
  phase: AuthPhase;
  /** The core's last answer, or null while none has been read (never asked, or it failed). */
  session: AuthSession | null;
  reason: SignedOutReason | null;
  /** The callback's `auth_error`, narrowed to a known code (or `"unknown"`) — never raw. */
  error: AuthErrorCode | null;
  /** Auto-redirect was due but one already left this tab moments ago: the loop guard held it,
   *  and the screen shows the button instead. */
  loopGuarded: boolean;
  /** Sign in was pressed while the core was not answering; nothing was navigated to. */
  unreachable: boolean;
  /** Read the session once, before the app mounts. Idempotent — the first call wins. */
  boot: () => Promise<void>;
  /** Ask again (a tab coming back into view, or a 401 with no session known). Never redirects. */
  refresh: () => Promise<void>;
  /** A `/platform/` request came back 401. */
  reportUnauthenticated: () => void;
  /** The Sign in button: re-check, then hand the window to the core's login route. */
  signIn: () => Promise<void>;
  /** Settings → Sign out. Rejects (and stays signed in) if the core could not be told. */
  signOut: () => Promise<void>;
  /** The page came back from the back-forward cache — typically Back from the provider. */
  resumeFromHistory: () => void;
}

const INITIAL = {
  phase: "idle",
  session: null,
  reason: null,
  error: null,
  loopGuarded: false,
  unreachable: false,
} as const satisfies Partial<AuthState>;

// One session read in flight at a time: the boot check, a 401's re-check and a tab's re-check
// can all ask at once, and they should share one answer rather than race three.
let inflight: Promise<AuthSession | null> | null = null;

async function readSession(): Promise<AuthSession | null> {
  try {
    return await api.authSession();
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return NO_SIGN_IN;
    // Unreachable, restarting, or an answer this build cannot read: unknown, not "signed out".
    return null;
  }
}

function loadSession(): Promise<AuthSession | null> {
  inflight ??= readSession().finally(() => {
    inflight = null;
  });
  return inflight;
}

function withoutUser(session: AuthSession): AuthSession {
  return { ...session, signed_in: false, user: null, expires_at: null };
}

export const useAuth = create<AuthState>()((set, get) => {
  const enterApp = () =>
    set({ phase: "app", reason: null, error: null, loopGuarded: false, unreachable: false });

  /** Replace the app with the sign-in screen — or, when the operator turned auto-redirect on,
   *  go straight to the provider. Auto-redirect is skipped after a failed attempt (an error to
   *  show), after an explicit sign-out in this tab, and — the loop guard — when this tab was
   *  sent away automatically within the last 30 s and came back still signed out. */
  const leaveApp = (reason: SignedOutReason) => {
    const { session, error } = get();
    const wanted =
      session?.auto_redirect === true &&
      error === null &&
      reason !== "signed-out" &&
      !signedOutHere();
    if (wanted && !autoRedirectedRecently()) {
      markAutoRedirect();
      set({ phase: "redirecting", reason, loopGuarded: false, unreachable: false });
      authNav.assign(loginUrl(currentReturnPath()));
      return;
    }
    set({ phase: "signed-out", reason, loopGuarded: wanted, unreachable: false });
  };

  return {
    ...INITIAL,

    boot: async () => {
      if (get().phase !== "idle") return;
      set({ phase: "checking", error: takeAuthErrorFromUrl() });
      const patience = setTimeout(() => {
        if (get().phase === "checking") set({ phase: "app" });
      }, SESSION_WAIT_MS);
      const session = await loadSession();
      clearTimeout(patience);
      if (session !== null) set({ session });
      const { phase } = get();
      if (session !== null && signInRequired(session)) {
        if (phase === "checking" || phase === "app") leaveApp("arrived");
      } else if (phase === "checking") {
        enterApp();
      }
    },

    refresh: async () => {
      const before = get().session;
      const session = await loadSession();
      if (session === null) return;
      set({ session });
      const { phase } = get();
      if (signInRequired(session)) {
        if (phase === "app") leaveApp(before?.signed_in ? "expired" : "arrived");
      } else if (phase === "signed-out") {
        enterApp();
      }
    },

    reportUnauthenticated: () => {
      const { phase, session } = get();
      if (phase !== "app") return;
      if (session !== null && session.mode !== "none") {
        set({ session: withoutUser(session) });
        leaveApp(session.signed_in ? "expired" : "arrived");
        return;
      }
      void get().refresh();
    },

    signIn: async () => {
      if (get().phase === "redirecting") return;
      clearSignedOutMark();
      set({ phase: "redirecting", error: null, loopGuarded: false, unreachable: false });
      // Check first: a top-level navigation to a core that is down ends on the proxy's bare
      // error page — in an installed PWA, a page with no way back.
      const session = await loadSession();
      if (session === null) {
        set({ phase: "signed-out", unreachable: true });
        return;
      }
      set({ session });
      if (!signInRequired(session)) {
        enterApp(); // signed in meanwhile (another tab), or sign-in was switched off
        return;
      }
      authNav.assign(loginUrl(currentReturnPath()));
    },

    signOut: async () => {
      // Marked before the request, so a 401 racing it can never bounce this tab to the provider.
      markSignedOut();
      try {
        await api.authLogout();
      } catch (err) {
        // A session that had already ended is signed out all the same.
        if (!(err instanceof ApiError && err.status === 401)) {
          clearSignedOutMark();
          throw err;
        }
      }
      // A fresh start, not a return: signing back in lands on the home screen.
      window.history.replaceState(null, "", "/");
      const { session } = get();
      set({
        session: session === null ? null : withoutUser(session),
        phase: "signed-out",
        reason: "signed-out",
        error: null,
        loopGuarded: false,
        unreachable: false,
      });
    },

    resumeFromHistory: () => {
      if (get().phase !== "redirecting") return;
      set({ phase: "signed-out", loopGuarded: autoRedirectedRecently() });
      void get().refresh();
    },
  };
});

// Every transport's 401 reaches the gate through this one seam (lib/auth.ts), which keeps the
// transport free of any import of this store.
setUnauthenticatedHandler(() => useAuth.getState().reportUnauthenticated());

/** Back to a fresh page load: the gate boots again on its next mount. Tests only — a real page
 *  starts here and never returns. */
export function resetAuthState(): void {
  inflight = null;
  useAuth.setState({ ...INITIAL });
}
