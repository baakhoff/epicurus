import { describe, expect, it } from "vitest";

import {
  APP_ORIGIN,
  decideNavigation,
  decideWindowOpen,
  isAppPage,
  isOnServer,
  offOriginHost,
} from "../../src/main/navigation";

const SERVER = "https://epicurus.example.com";

describe("decideNavigation (top-level, started by the page)", () => {
  it.each([
    `${SERVER}/platform/v1/auth/login?next=%2F`,
    "https://id.example.com/authorize?client_id=x",
    "https://accounts.google.com/o/oauth2/v2/auth?x=1",
    "http://nas.lan:8080/",
  ])("allows %s — the sign-in and connect flows leave the server and come back", (url) => {
    expect(decideNavigation(url)).toBe("allow");
  });

  it.each([
    "file:///etc/passwd",
    "data:text/html,<script>1</script>",
    "javascript:alert(1)",
    `${APP_ORIGIN}/connect.html`,
    "about:blank",
    "ms-outlook://compose",
    "chrome://settings",
    "https://user:pw@evil.example/",
    "not a url",
  ])("refuses %s", (url) => {
    expect(decideNavigation(url)).toBe("refuse");
  });
});

describe("decideWindowOpen (target=_blank, window.open)", () => {
  it.each(["https://docs.example.com/x", "http://example.com", "mailto:someone@example.com"])(
    "sends %s to the system",
    (url) => expect(decideWindowOpen(url)).toBe("open-external"),
  );
  it.each(["file:///C:/Windows/System32/calc.exe", "javascript:void 0", "smb://host/share", "", "%%"])(
    "drops %j",
    (url) => expect(decideWindowOpen(url)).toBe("refuse"),
  );
});

describe("offOriginHost", () => {
  it("is null on the server, including deep links and other ports' absence", () => {
    expect(offOriginHost(`${SERVER}/settings`, SERVER)).toBeNull();
  });

  it("names the host of any other http(s) page", () => {
    expect(offOriginHost("https://id.example.com/login", SERVER)).toBe("id.example.com");
    expect(offOriginHost("https://epicurus.example.com:8443/", SERVER)).toBe(
      "epicurus.example.com:8443",
    );
    expect(offOriginHost("http://epicurus.example.com/", SERVER)).toBe("epicurus.example.com");
  });

  it("is null on the app's own pages and on non-web URLs", () => {
    expect(offOriginHost(`${APP_ORIGIN}/offline.html`, SERVER)).toBeNull();
    expect(offOriginHost("about:blank", SERVER)).toBeNull();
    expect(offOriginHost("garbage", SERVER)).toBeNull();
  });

  it("names the host when no server is saved", () => {
    expect(offOriginHost("https://x.example/", null)).toBe("x.example");
  });
});

describe("isAppPage and isOnServer", () => {
  it("recognises only the app's own scheme and host", () => {
    expect(isAppPage(`${APP_ORIGIN}/connect.html?x=1`)).toBe(true);
    expect(isAppPage("epicurus-app://other/connect.html")).toBe(false);
    expect(isAppPage("https://local/connect.html")).toBe(false);
  });

  it("compares origins exactly", () => {
    expect(isOnServer(`${SERVER}/a`, SERVER)).toBe(true);
    expect(isOnServer("https://epicurus.example.com.evil.example/", SERVER)).toBe(false);
    expect(isOnServer(`${SERVER}/`, null)).toBe(false);
  });
});
