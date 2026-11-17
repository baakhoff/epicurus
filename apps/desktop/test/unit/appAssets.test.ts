import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { APP_ASSETS, APP_CSP, resolveAppAsset } from "../../src/main/appAssets";

const ROOT = join("/opt", "epicurus", "renderer");

describe("resolveAppAsset", () => {
  it("serves each listed file with its content type", () => {
    for (const [name, type] of Object.entries(APP_ASSETS)) {
      expect(resolveAppAsset(`epicurus-app://local/${name}?x=1#y`, ROOT)).toEqual({
        file: join(ROOT, name),
        contentType: type,
      });
    }
  });

  it.each([
    "epicurus-app://local/",
    "epicurus-app://local/../main/main.js",
    "epicurus-app://local/%2e%2e/%2e%2e/etc/passwd",
    "epicurus-app://local/..%2fpreload%2fpreload.js",
    "epicurus-app://local/connect.html/x",
    "epicurus-app://local/constructor",
    "epicurus-app://local/__proto__",
    "epicurus-app://local/hasOwnProperty",
    "epicurus-app://other/connect.html",
    "https://local/connect.html",
    "not a url",
  ])("refuses %s", (url) => {
    expect(resolveAppAsset(url, ROOT)).toBeNull();
  });

  it("allows no inline script and nothing from elsewhere", () => {
    expect(APP_CSP).toContain("default-src 'none'");
    expect(APP_CSP).toContain("script-src 'self'");
    expect(APP_CSP).not.toContain("unsafe-inline");
    expect(APP_CSP).toContain("frame-ancestors 'none'");
  });
});
