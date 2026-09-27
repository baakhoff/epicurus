import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "@/App";
import { AccountCard } from "@/components/AccountCard";
import { AuthGate } from "@/components/AuthGate";
import { api } from "@/lib/api";
import { authNav } from "@/lib/auth";
import type { AuthSession } from "@/lib/contracts";
import { epFetch } from "@/lib/http";
import { resetAuthState, useAuth } from "@/stores/auth";
import { useConnection } from "@/stores/connection";

// The web half of sign-in (#969), rendered: the gate, the sign-in screen, the 401 round trip
// through the real App, and Settings → Account.

vi.mock("virtual:pwa-register/react", () => ({
  useRegisterSW: () => ({ needRefresh: [false], updateServiceWorker: vi.fn() }),
}));

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
  user: { subject: "u-1", email: "ada@example.com", name: "Ada Lovelace", groups: [] },
});
const NONE = session({ mode: "none", provider_name: null });

let assign: ReturnType<typeof vi.spyOn>;
let authSession: ReturnType<typeof vi.spyOn>;

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function renderGate(child: ReactNode = <div>THE APP</div>) {
  const qc = client();
  return render(
    <QueryClientProvider client={qc}>
      <AuthGate>{child}</AuthGate>
    </QueryClientProvider>,
  );
}

/** The data plane the real Shell reaches for on a light route. */
function quietShell() {
  vi.spyOn(api, "modules").mockResolvedValue([]);
  vi.spyOn(api, "power").mockResolvedValue({ state: "idle" });
  vi.spyOn(api, "pageOrder").mockResolvedValue({ order: [] });
  vi.spyOn(api, "notificationsUnreadCount").mockResolvedValue({ count: 0 });
}

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
  window.history.replaceState(null, "", "/");
});

describe("the gate", () => {
  it("renders nothing of the app while the session check is out", async () => {
    let answer: (s: AuthSession) => void = () => {};
    authSession.mockReturnValue(new Promise<AuthSession>((r) => (answer = r)));
    renderGate();
    expect(screen.getByRole("status", { name: "Opening epicurus" })).toBeInTheDocument();
    expect(screen.queryByText("THE APP")).not.toBeInTheDocument();
    await act(async () => answer(NONE));
    expect(await screen.findByText("THE APP")).toBeInTheDocument();
  });

  it("renders the app with sign-in off, and no sign-in UI anywhere", async () => {
    authSession.mockResolvedValue(NONE);
    renderGate();
    expect(await screen.findByText("THE APP")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /sign in/i })).not.toBeInTheDocument();
  });

  it("renders the app when signed in", async () => {
    authSession.mockResolvedValue(SIGNED_IN);
    renderGate();
    expect(await screen.findByText("THE APP")).toBeInTheDocument();
  });

  it("renders the app when the core can't be reached (the banner explains it, as ever)", async () => {
    authSession.mockRejectedValue(new TypeError("Failed to fetch"));
    renderGate();
    expect(await screen.findByText("THE APP")).toBeInTheDocument();
  });

  it("renders the sign-in screen, not the app, when signed out", async () => {
    authSession.mockResolvedValue(session());
    renderGate();
    expect(await screen.findByRole("heading", { name: "Sign in to epicurus" })).toBeInTheDocument();
    expect(screen.queryByText("THE APP")).not.toBeInTheDocument();
  });

  it("never mounts the shell's data tree for a signed-out browser — no burst of 401s", async () => {
    quietShell();
    authSession.mockResolvedValue(session());
    render(<App />);
    await screen.findByRole("heading", { name: "Sign in to epicurus" });
    expect(api.modules).not.toHaveBeenCalled();
    expect(api.power).not.toHaveBeenCalled();
    expect(api.notificationsUnreadCount).not.toHaveBeenCalled();
  });
});

describe("the sign-in screen", () => {
  it("names the provider on the button and signs in with next = where the browser is", async () => {
    window.history.replaceState(null, "", "/m/calendar/calendar?event=e1");
    authSession.mockResolvedValue(session());
    renderGate();
    fireEvent.click(await screen.findByRole("button", { name: "Sign in with Pocket ID" }));
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith(
        "/platform/v1/auth/login?next=%2Fm%2Fcalendar%2Fcalendar%3Fevent%3De1",
      ),
    );
  });

  it("says just 'Sign in' when the operator gave the provider no name", async () => {
    authSession.mockResolvedValue(session({ provider_name: null }));
    renderGate();
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("renders an auth_error as a sentence, strips the param, and leaves it out of next", async () => {
    window.history.replaceState(null, "", "/settings?auth_error=not_allowed");
    authSession.mockResolvedValue(session({ auto_redirect: true }));
    renderGate();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Your account isn't allowed to use this epicurus. Ask whoever runs it to add you.",
    );
    expect(window.location.search).toBe("");
    expect(assign).not.toHaveBeenCalled(); // an error is never auto-redirected past

    authSession.mockResolvedValue(session());
    fireEvent.click(screen.getByRole("button", { name: "Sign in with Pocket ID" }));
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("/platform/v1/auth/login?next=%2Fsettings"),
    );
  });

  it("renders the groups-claim failure with the operator's fix", async () => {
    window.history.replaceState(null, "", "/?auth_error=groups_claim_missing");
    authSession.mockResolvedValue(session());
    renderGate();
    expect(await screen.findByRole("alert")).toHaveTextContent(/groups scope/);
  });

  it("renders an unknown code as a generic sentence, never the raw value", async () => {
    window.history.replaceState(null, "", "/?auth_error=%3Cb%3Ehello%3C%2Fb%3E");
    authSession.mockResolvedValue(session());
    renderGate();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Signing in didn't work.");
    expect(alert.textContent).not.toContain("hello");
  });

  it("auto-redirects when the operator asked for it", async () => {
    authSession.mockResolvedValue(session({ auto_redirect: true }));
    renderGate();
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/platform/v1/auth/login?next=%2F"));
    expect(screen.getByRole("button", { name: /Taking you to Pocket ID/ })).toBeDisabled();
  });

  it("shows the button, with a note, when the loop guard holds a second auto-redirect", async () => {
    sessionStorage.setItem("epicurus-auth-auto-redirect-at", String(Date.now() - 2_000));
    authSession.mockResolvedValue(session({ auto_redirect: true }));
    renderGate();
    expect(await screen.findByRole("button", { name: "Sign in with Pocket ID" })).toBeEnabled();
    expect(screen.getByRole("status")).toHaveTextContent("Signing in didn't finish");
    expect(assign).not.toHaveBeenCalled();
  });

  it("says so when Sign in is pressed with the core unreachable", async () => {
    authSession.mockResolvedValueOnce(session());
    renderGate();
    const button = await screen.findByRole("button", { name: "Sign in with Pocket ID" });
    authSession.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    fireEvent.click(button);
    expect(await screen.findByRole("alert")).toHaveTextContent("Can't reach epicurus");
    expect(assign).not.toHaveBeenCalled();
  });

  it("enters the app when a returning tab finds it was signed in elsewhere", async () => {
    authSession.mockResolvedValueOnce(session());
    renderGate();
    await screen.findByRole("heading", { name: "Sign in to epicurus" });
    authSession.mockResolvedValueOnce(SIGNED_IN);
    await act(async () => document.dispatchEvent(new Event("visibilitychange")));
    expect(await screen.findByText("THE APP")).toBeInTheDocument();
    expect(assign).not.toHaveBeenCalled();
  });
});

describe("a 401 mid-session, through the real App", () => {
  it("replaces the shell with the sign-in screen and comes back to where the user was", async () => {
    quietShell();
    window.history.replaceState(null, "", "/m/none/none?view=week");
    authSession.mockResolvedValue(SIGNED_IN);
    render(<App />);
    await screen.findByRole("navigation", { name: "Primary" });

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Sign in to continue.", code: "unauthenticated" }), {
          status: 401,
        }),
      ),
    );
    await act(async () => {
      await epFetch("/platform/v1/power");
    });

    expect(await screen.findByRole("status")).toHaveTextContent("Your session has ended");
    expect(screen.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
    // The connection banner reads a 401 as an answer, not an outage.
    expect(useConnection.getState().coreDown).toBe(false);

    authSession.mockResolvedValue(session());
    fireEvent.click(screen.getByRole("button", { name: "Sign in with Pocket ID" }));
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith(
        "/platform/v1/auth/login?next=%2Fm%2Fnone%2Fnone%3Fview%3Dweek",
      ),
    );
  });

  it("carries a share (/?share=1) through the round trip without consuming it", async () => {
    quietShell();
    window.history.replaceState(null, "", "/?share=1");
    authSession.mockResolvedValue(session());
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Sign in with Pocket ID" }));
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("/platform/v1/auth/login?next=%2F%3Fshare%3D1"),
    );
    // The chat screen that consumes (and strips) the share never mounted.
    expect(window.location.search).toBe("?share=1");
  });

  it("carries a notification's deep link through the round trip", async () => {
    quietShell();
    window.history.replaceState(null, "", "/notifications?id=n-42");
    authSession.mockResolvedValue(session());
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Sign in with Pocket ID" }));
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith(
        "/platform/v1/auth/login?next=%2Fnotifications%3Fid%3Dn-42",
      ),
    );
  });
});

describe("Settings → Account", () => {
  function renderCard() {
    const qc = client();
    return render(
      <QueryClientProvider client={qc}>
        <AuthGate>
          <AccountCard />
        </AuthGate>
      </QueryClientProvider>,
    );
  }

  it("is absent with sign-in off", async () => {
    authSession.mockResolvedValue(NONE);
    renderCard();
    await waitFor(() => expect(authSession).toHaveBeenCalledTimes(2)); // the gate, then the card
    expect(screen.queryByText("Account")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /sign out/i })).not.toBeInTheDocument();
  });

  it("names who is signed in, and through which provider", async () => {
    authSession.mockResolvedValue(SIGNED_IN);
    renderCard();
    expect(await screen.findByText(/Signed in as/)).toHaveTextContent(
      "Signed in as Ada Lovelace · ada@example.com",
    );
    expect(screen.getByText("through Pocket ID")).toBeInTheDocument();
  });

  it("stays graceful when the provider sent no name, or neither name nor email", async () => {
    authSession.mockResolvedValue(
      session({ signed_in: true, user: { subject: "u-1", email: "ada@example.com" } }),
    );
    const { unmount } = renderCard();
    expect(await screen.findByText(/Signed in as/)).toHaveTextContent(
      /^Signed in as ada@example\.com$/,
    );
    unmount();

    resetAuthState();
    authSession.mockResolvedValue(session({ signed_in: true, user: { subject: "u-9" } }));
    renderCard();
    expect(await screen.findByText("Signed in")).toBeInTheDocument();
    expect(screen.getByText("account u-9")).toBeInTheDocument();
  });

  it("signs out to the signed-out screen, with auto-redirect held off", async () => {
    window.history.replaceState(null, "", "/settings");
    authSession.mockResolvedValue({ ...SIGNED_IN, auto_redirect: true });
    const logout = vi.spyOn(api, "authLogout").mockResolvedValue({ signed_out: true });
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: /sign out/i }));

    expect(await screen.findByRole("status")).toHaveTextContent("You're signed out.");
    expect(logout).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Sign in with Pocket ID" })).toBeEnabled();
    expect(assign).not.toHaveBeenCalled();
    expect(useAuth.getState().reason).toBe("signed-out");
  });
});
