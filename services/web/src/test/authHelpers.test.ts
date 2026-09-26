import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  AUTH_ERROR_CODES,
  AUTO_REDIRECT_GUARD_MS,
  authErrorMessage,
  autoRedirectedRecently,
  currentReturnPath,
  isSignInEnforcedPath,
  isUnauthenticatedError,
  loginUrl,
  markAutoRedirect,
  retryUnlessSignedOut,
  safeNext,
  takeAuthErrorFromUrl,
  toAuthErrorCode,
} from "@/lib/auth";
import { ApiError } from "@/lib/api";
import { AuthSession, signInRequired } from "@/lib/contracts";

beforeEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, "", "/");
});
afterEach(() => window.history.replaceState(null, "", "/"));

describe("the session contract (#969)", () => {
  it("parses a signed-in oidc session and tolerates fields it has never heard of", () => {
    const session = AuthSession.parse({
      mode: "oidc",
      signed_in: true,
      provider_name: "Pocket ID",
      auto_redirect: false,
      user: { subject: "u1", email: "ada@example.com", name: "Ada", groups: ["family"], picture: "x" },
      expires_at: "2026-10-26T00:00:00Z",
      workspace: "future-field",
    });
    expect(session.user?.name).toBe("Ada");
    expect(session).not.toHaveProperty("workspace");
    expect(signInRequired(session)).toBe(false);
  });

  it("parses the mode-none answer, with nullish user fields", () => {
    const session = AuthSession.parse({
      mode: "none",
      signed_in: false,
      provider_name: null,
      auto_redirect: false,
      user: null,
      expires_at: null,
    });
    expect(signInRequired(session)).toBe(false);
  });

  it("requires sign-in for any mode but none when there is no session — unknown modes included", () => {
    const base = { signed_in: false, user: null };
    expect(signInRequired(AuthSession.parse({ ...base, mode: "oidc" }))).toBe(true);
    expect(signInRequired(AuthSession.parse({ ...base, mode: "something-new" }))).toBe(true);
    // auto_redirect absent means off — a default for tolerance, never an invention.
    expect(AuthSession.parse({ ...base, mode: "oidc" }).auto_redirect).toBe(false);
  });

  it("accepts a user with only a subject", () => {
    const session = AuthSession.parse({ mode: "oidc", signed_in: true, user: { subject: "s" } });
    expect(session.user?.email).toBeUndefined();
  });
});

describe("auth_error codes", () => {
  it("has a distinct human sentence for every known code", () => {
    const sentences = AUTH_ERROR_CODES.map((c) => authErrorMessage(c));
    expect(new Set(sentences).size).toBe(AUTH_ERROR_CODES.length);
    expect(authErrorMessage("not_allowed")).toMatch(/isn't allowed.*ask whoever runs it/i);
    expect(authErrorMessage("groups_claim_missing")).toMatch(/groups scope/i);
    expect(authErrorMessage("provider_unreachable")).toMatch(/couldn't be reached/i);
  });

  it("narrows anything unknown to a generic sentence, never echoing the raw value", () => {
    const code = toAuthErrorCode("<script>alert(1)</script>");
    expect(code).toBe("unknown");
    expect(authErrorMessage(code)).not.toContain("script");
  });

  it("takes auth_error off the address bar with a replace, keeping the rest of the URL", () => {
    window.history.replaceState({ k: 1 }, "", "/settings?tab=x&auth_error=state_mismatch#h");
    const before = window.history.length;
    expect(takeAuthErrorFromUrl()).toBe("state_mismatch");
    expect(window.location.pathname + window.location.search + window.location.hash).toBe(
      "/settings?tab=x#h",
    );
    expect(window.history.length).toBe(before); // replaced, not pushed
    expect(window.history.state).toEqual({ k: 1 });
    expect(takeAuthErrorFromUrl()).toBeNull();
  });
});

describe("next — where sign-in comes back to", () => {
  it("is the current path and query, minus auth_error", () => {
    expect(currentReturnPath({ pathname: "/m/mail/inbox", search: "?thread=t1" })).toBe(
      "/m/mail/inbox?thread=t1",
    );
    expect(currentReturnPath({ pathname: "/", search: "?share=1&auth_error=x" })).toBe(
      "/?share=1",
    );
    expect(currentReturnPath({ pathname: "/", search: "?auth_error=x" })).toBe("/");
  });

  it("never names anything but a same-origin app path", () => {
    expect(safeNext("//evil.example/x")).toBe("/");
    expect(safeNext("/\\evil.example")).toBe("/");
    expect(safeNext("https://evil.example")).toBe("/");
    expect(safeNext("/platform/v1/auth/login")).toBe("/");
    expect(safeNext("/settings")).toBe("/settings");
  });

  it("is URI-encoded into the core's login route", () => {
    expect(loginUrl("/?share=1")).toBe("/platform/v1/auth/login?next=%2F%3Fshare%3D1");
    expect(loginUrl("/m/calendar/calendar?event=a&b=c")).toBe(
      "/platform/v1/auth/login?next=%2Fm%2Fcalendar%2Fcalendar%3Fevent%3Da%26b%3Dc",
    );
  });
});

describe("401 plumbing", () => {
  it("treats platform routes as enforced, the sign-in routes themselves as not", () => {
    expect(isSignInEnforcedPath("/platform/v1/power")).toBe(true);
    expect(isSignInEnforcedPath("/platform/v1/auth/session")).toBe(false);
    expect(isSignInEnforcedPath("/platform/v1/auth/logout")).toBe(false);
    expect(isSignInEnforcedPath("/assets/app.js")).toBe(false);
  });

  it("never retries a 401, and keeps the one-retry default for everything else", () => {
    const unauth = new ApiError(401, "Sign in to continue.");
    expect(isUnauthenticatedError(unauth)).toBe(true);
    expect(isUnauthenticatedError(Object.assign(new Error("x"), { status: 401 }))).toBe(true);
    expect(retryUnlessSignedOut(0, unauth)).toBe(false);
    expect(retryUnlessSignedOut(0, new ApiError(500, "boom"))).toBe(true);
    expect(retryUnlessSignedOut(1, new ApiError(500, "boom"))).toBe(false);
    expect(retryUnlessSignedOut(0, new TypeError("Failed to fetch"))).toBe(true);
  });
});

describe("the auto-redirect loop guard", () => {
  it("holds a second automatic redirect inside the window and releases it after", () => {
    const t0 = 1_000_000;
    expect(autoRedirectedRecently(t0)).toBe(false);
    markAutoRedirect(t0);
    expect(autoRedirectedRecently(t0 + 5_000)).toBe(true);
    expect(autoRedirectedRecently(t0 + AUTO_REDIRECT_GUARD_MS + 1)).toBe(false);
  });
});
