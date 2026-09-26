import { describe, expect, it } from "vitest";

import { swRoute } from "@/lib/swRoute";

// The service worker's whole fetch decision (#969). The bug it fixes: every navigate-mode
// request — `/platform/` included — was answered with the cached app shell, so the
// connected-account OAuth callback (Google's redirect back) never reached the core once the
// PWA's worker was installed, and the sign-in login/callback would have hit the same wall.
const ORIGIN = "https://epicurus.example";
const nav = (path: string, method = "GET") => ({ method, mode: "navigate", url: ORIGIN + path });

describe("swRoute", () => {
  it("serves the app shell for a navigation to an app path, deep links included", () => {
    expect(swRoute(nav("/"))).toBe("app-shell");
    expect(swRoute(nav("/settings"))).toBe("app-shell");
    expect(swRoute(nav("/m/calendar/calendar?event=abc"))).toBe("app-shell");
    // The share target's Post-Redirect-Get destination.
    expect(swRoute(nav("/?share=1"))).toBe("app-shell");
    // A sign-in failure comes back as a top-level navigation to the app.
    expect(swRoute(nav("/?auth_error=not_allowed"))).toBe("app-shell");
  });

  it("lets the connected-account OAuth callback through to the network", () => {
    expect(swRoute(nav("/platform/v1/oauth/callback?code=x&state=y"))).toBe("pass-through");
  });

  it("lets the sign-in login and callback navigations through to the network", () => {
    expect(swRoute(nav("/platform/v1/auth/login?next=%2F"))).toBe("pass-through");
    expect(swRoute(nav("/platform/v1/auth/callback?code=x&state=y"))).toBe("pass-through");
  });

  it("lets any other platform URL opened in a tab through", () => {
    expect(swRoute(nav("/platform/v1/files/download?path=a.pdf"))).toBe("pass-through");
  });

  it("does not mistake a lookalike path for the platform", () => {
    expect(swRoute(nav("/platformer"))).toBe("app-shell");
  });

  it("never answers a non-navigation request (the app's own fetch/SSE calls)", () => {
    expect(swRoute({ method: "GET", mode: "cors", url: `${ORIGIN}/platform/v1/power` })).toBe(
      "pass-through",
    );
    expect(swRoute({ method: "GET", mode: "same-origin", url: `${ORIGIN}/assets/x.js` })).toBe(
      "pass-through",
    );
  });

  it("handles the share POST first, even when a browser reports it as a navigation", () => {
    expect(swRoute(nav("/share-target", "POST"))).toBe("share-target");
    expect(swRoute({ method: "POST", mode: "cors", url: `${ORIGIN}/share-target` })).toBe(
      "share-target",
    );
    // A GET to the same path is just an app path.
    expect(swRoute(nav("/share-target"))).toBe("app-shell");
  });
});
