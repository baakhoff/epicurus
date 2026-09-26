import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "@/lib/api";
import { authNav, markAutoRedirect, signedOutHere } from "@/lib/auth";
import type { AuthSession } from "@/lib/contracts";
import { epFetch } from "@/lib/http";
import { SESSION_WAIT_MS, resetAuthState, useAuth } from "@/stores/auth";
import { useConnection } from "@/stores/connection";

// The auth gate's state machine (#969), without rendering: boot, the 401 flip, sign-in,
// sign-out, and the transports that feed it.

const session = (over: Partial<AuthSession> = {}): AuthSession => ({
  mode: "oidc",
  signed_in: false,
  provider_name: "Pocket ID",
  auto_redirect: false,
  user: null,
  expires_at: null,
  ...over,
});
const SIGNED_IN = session({
  signed_in: true,
  user: { subject: "u1", email: "ada@example.com", name: "Ada", groups: [] },
});
const NONE = session({ mode: "none", provider_name: null });

let assign: ReturnType<typeof vi.spyOn>;
let authSession: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  resetAuthState();
  sessionStorage.clear();
  window.history.replaceState(null, "", "/");
  useConnection.setState({ online: true, coreDown: false, pendingDown: false, confirmedDown: false });
  assign = vi.spyOn(authNav, "assign").mockImplementation(() => {});
  authSession = vi.spyOn(api, "authSession");
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  window.history.replaceState(null, "", "/");
});

describe("boot", () => {
  it("opens the app when sign-in is off", async () => {
    authSession.mockResolvedValue(NONE);
    await useAuth.getState().boot();
    expect(useAuth.getState().phase).toBe("app");
  });

  it("opens the app when signed in", async () => {
    authSession.mockResolvedValue(SIGNED_IN);
    await useAuth.getState().boot();
    expect(useAuth.getState().phase).toBe("app");
  });

  it("shows the sign-in screen when sign-in is on and there is no session", async () => {
    authSession.mockResolvedValue(session());
    await useAuth.getState().boot();
    expect(useAuth.getState()).toMatchObject({ phase: "signed-out", reason: "arrived" });
    expect(assign).not.toHaveBeenCalled();
  });

  it("reads an older core's 404 as no sign-in at all", async () => {
    authSession.mockRejectedValue(new ApiError(404, "Not Found"));
    await useAuth.getState().boot();
    expect(useAuth.getState().phase).toBe("app");
    expect(useAuth.getState().session?.mode).toBe("none");
  });

  it("opens the app when the core cannot answer, as the shell always has", async () => {
    authSession.mockRejectedValue(new TypeError("Failed to fetch"));
    await useAuth.getState().boot();
    expect(useAuth.getState()).toMatchObject({ phase: "app", session: null });
  });

  it("stops waiting on a core that doesn't answer, and still flips on its late answer", async () => {
    vi.useFakeTimers();
    let answer: (s: AuthSession) => void = () => {};
    authSession.mockReturnValue(new Promise<AuthSession>((resolve) => (answer = resolve)));
    const booting = useAuth.getState().boot();
    expect(useAuth.getState().phase).toBe("checking");
    await vi.advanceTimersByTimeAsync(SESSION_WAIT_MS);
    expect(useAuth.getState().phase).toBe("app");
    answer(session());
    await booting;
    expect(useAuth.getState().phase).toBe("signed-out");
  });

  it("is idempotent — a second call (StrictMode's double effect) asks nothing", async () => {
    authSession.mockResolvedValue(NONE);
    await Promise.all([useAuth.getState().boot(), useAuth.getState().boot()]);
    expect(authSession).toHaveBeenCalledTimes(1);
  });

  it("captures auth_error as a narrowed code and strips it from the URL", async () => {
    window.history.replaceState(null, "", "/?auth_error=not_allowed");
    authSession.mockResolvedValue(session({ auto_redirect: true }));
    await useAuth.getState().boot();
    expect(useAuth.getState().error).toBe("not_allowed");
    expect(window.location.search).toBe("");
    // An error on screen is never bounced straight back to the provider.
    expect(assign).not.toHaveBeenCalled();
  });

  it("drops a stale auth_error silently when the browser is signed in after all", async () => {
    window.history.replaceState(null, "", "/?auth_error=state_mismatch");
    authSession.mockResolvedValue(SIGNED_IN);
    await useAuth.getState().boot();
    expect(useAuth.getState()).toMatchObject({ phase: "app", error: null });
    expect(window.location.search).toBe("");
  });
});

describe("auto-redirect", () => {
  it("goes straight to the provider with next = where the browser is", async () => {
    window.history.replaceState(null, "", "/m/tasks/board?x=1");
    authSession.mockResolvedValue(session({ auto_redirect: true }));
    await useAuth.getState().boot();
    expect(useAuth.getState().phase).toBe("redirecting");
    expect(assign).toHaveBeenCalledWith(
      "/platform/v1/auth/login?next=%2Fm%2Ftasks%2Fboard%3Fx%3D1",
    );
  });

  it("holds a second automatic redirect within 30 s and shows the button (loop guard)", async () => {
    markAutoRedirect(Date.now() - 5_000);
    authSession.mockResolvedValue(session({ auto_redirect: true }));
    await useAuth.getState().boot();
    expect(useAuth.getState()).toMatchObject({ phase: "signed-out", loopGuarded: true });
    expect(assign).not.toHaveBeenCalled();
  });

  it("redirects again once the guard window has passed", async () => {
    markAutoRedirect(Date.now() - 31_000);
    authSession.mockResolvedValue(session({ auto_redirect: true }));
    await useAuth.getState().boot();
    expect(assign).toHaveBeenCalledTimes(1);
  });

  it("stays off in a tab where the user just signed out", async () => {
    sessionStorage.setItem("epicurus-auth-signed-out", "1");
    authSession.mockResolvedValue(session({ auto_redirect: true }));
    await useAuth.getState().boot();
    expect(useAuth.getState().phase).toBe("signed-out");
    expect(assign).not.toHaveBeenCalled();
  });
});

describe("a 401 mid-session", () => {
  it("flips a signed-in app to the sign-in screen at once", async () => {
    authSession.mockResolvedValue(SIGNED_IN);
    await useAuth.getState().boot();
    useAuth.getState().reportUnauthenticated();
    expect(useAuth.getState()).toMatchObject({ phase: "signed-out", reason: "expired" });
    expect(useAuth.getState().session?.user).toBeNull();
  });

  it("asks the core first when no session was known, and flips only on its answer", async () => {
    authSession.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await useAuth.getState().boot();
    expect(useAuth.getState().phase).toBe("app");

    authSession.mockResolvedValueOnce(session());
    useAuth.getState().reportUnauthenticated();
    await vi.waitFor(() => expect(useAuth.getState().phase).toBe("signed-out"));
  });

  it("stays in the app when the re-check says sign-in is off (a 401 from something in front)", async () => {
    authSession.mockResolvedValue(NONE);
    await useAuth.getState().boot();
    useAuth.getState().reportUnauthenticated();
    await vi.waitFor(() => expect(authSession).toHaveBeenCalledTimes(2));
    expect(useAuth.getState().phase).toBe("app");
  });

  it("arrives through epFetch — and counts as reachable, not as the core being down", async () => {
    authSession.mockResolvedValue(SIGNED_IN);
    await useAuth.getState().boot();
    useConnection.setState({ coreDown: true });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Sign in to continue.", code: "unauthenticated" }), {
          status: 401,
        }),
      ),
    );
    await epFetch("/platform/v1/notifications/unread-count");
    expect(useConnection.getState().coreDown).toBe(false);
    expect(useAuth.getState().phase).toBe("signed-out");
  });

  it("is not read from the sign-in routes themselves", async () => {
    authSession.mockResolvedValue(SIGNED_IN);
    await useAuth.getState().boot();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 401 })));
    await epFetch("/platform/v1/auth/session");
    expect(useAuth.getState().phase).toBe("app");
  });

  it("is ignored before the app has mounted", () => {
    useAuth.setState({ phase: "checking", session: SIGNED_IN });
    useAuth.getState().reportUnauthenticated();
    expect(useAuth.getState().phase).toBe("checking");
  });
});

describe("sign in", () => {
  it("re-checks, then navigates to the login route with next", async () => {
    window.history.replaceState(null, "", "/?share=1");
    useAuth.setState({ phase: "signed-out", session: session() });
    authSession.mockResolvedValue(session());
    await useAuth.getState().signIn();
    expect(assign).toHaveBeenCalledWith("/platform/v1/auth/login?next=%2F%3Fshare%3D1");
    expect(useAuth.getState().phase).toBe("redirecting");
  });

  it("doesn't navigate into a dead core — it says so instead", async () => {
    useAuth.setState({ phase: "signed-out", session: session() });
    authSession.mockRejectedValue(new TypeError("Failed to fetch"));
    await useAuth.getState().signIn();
    expect(assign).not.toHaveBeenCalled();
    expect(useAuth.getState()).toMatchObject({ phase: "signed-out", unreachable: true });
  });

  it("walks straight in when another tab signed in meanwhile", async () => {
    useAuth.setState({ phase: "signed-out", session: session() });
    authSession.mockResolvedValue(SIGNED_IN);
    await useAuth.getState().signIn();
    expect(assign).not.toHaveBeenCalled();
    expect(useAuth.getState().phase).toBe("app");
  });

  it("un-freezes a tab restored from the back-forward cache mid-redirect", () => {
    authSession.mockResolvedValue(session());
    useAuth.setState({ phase: "redirecting", session: session() });
    useAuth.getState().resumeFromHistory();
    expect(useAuth.getState().phase).toBe("signed-out");
  });
});

describe("sign out", () => {
  it("ends the session, lands signed out on /, and holds auto-redirect off", async () => {
    window.history.replaceState(null, "", "/settings");
    useAuth.setState({ phase: "app", session: session({ ...SIGNED_IN, auto_redirect: true }) });
    const logout = vi.spyOn(api, "authLogout").mockResolvedValue({ signed_out: true });
    await useAuth.getState().signOut();
    expect(logout).toHaveBeenCalledTimes(1);
    expect(useAuth.getState()).toMatchObject({ phase: "signed-out", reason: "signed-out" });
    expect(window.location.pathname).toBe("/");
    expect(signedOutHere()).toBe(true);
    expect(assign).not.toHaveBeenCalled();
  });

  it("treats an already-ended session as signed out", async () => {
    useAuth.setState({ phase: "app", session: SIGNED_IN });
    vi.spyOn(api, "authLogout").mockRejectedValue(new ApiError(401, "Sign in to continue."));
    await useAuth.getState().signOut();
    expect(useAuth.getState().phase).toBe("signed-out");
  });

  it("stays signed in, and says why, when the core could not be told", async () => {
    useAuth.setState({ phase: "app", session: SIGNED_IN });
    vi.spyOn(api, "authLogout").mockRejectedValue(new ApiError(500, "boom"));
    await expect(useAuth.getState().signOut()).rejects.toThrow("boom");
    expect(useAuth.getState().phase).toBe("app");
    expect(signedOutHere()).toBe(false);
  });
});
