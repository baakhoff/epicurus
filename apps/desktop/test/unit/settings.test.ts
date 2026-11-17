import { mkdtempSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  MIN_HEIGHT,
  MIN_WIDTH,
  SETTINGS_FILE,
  SettingsStore,
  defaultSettings,
  parseSettings,
} from "../../src/main/settings";

const dir = () => mkdtempSync(join(tmpdir(), "epicurus-settings-"));

describe("parseSettings", () => {
  it("falls back to defaults for anything that is not an object", () => {
    for (const raw of [null, 3, "x", [], undefined]) expect(parseSettings(raw)).toEqual(defaultSettings());
  });

  it("re-normalises a hand-edited server address", () => {
    expect(parseSettings({ server: { url: "Example.com/deep" } }).server).toEqual({
      url: "https://example.com/",
      confirmedInsecure: false,
    });
  });

  it("drops a server address that is not one", () => {
    expect(parseSettings({ server: { url: "file:///etc" } }).server).toBeNull();
    expect(parseSettings({ server: { url: 42 } }).server).toBeNull();
    expect(parseSettings({ server: "https://example.com" }).server).toBeNull();
  });

  it("keeps confirmedInsecure only when it is literally true", () => {
    expect(parseSettings({ server: { url: "http://nas.lan", confirmedInsecure: "yes" } }).server)
      .toEqual({ url: "http://nas.lan/", confirmedInsecure: false });
    expect(parseSettings({ server: { url: "http://nas.lan", confirmedInsecure: true } }).server)
      .toEqual({ url: "http://nas.lan/", confirmedInsecure: true });
  });

  it("validates the window state field by field and enforces the minimum size", () => {
    expect(parseSettings({ window: { width: 100, height: 50.6, maximized: 1 } }).window).toEqual({
      width: MIN_WIDTH,
      height: MIN_HEIGHT,
      maximized: false,
    });
    expect(parseSettings({ window: { width: 900, height: 700, x: -20, y: 40 } }).window).toEqual({
      width: 900,
      height: 700,
      x: -20,
      y: 40,
      maximized: false,
    });
    // A position needs both coordinates.
    expect(parseSettings({ window: { width: 900, height: 700, x: 5 } }).window).not.toHaveProperty("x");
    expect(parseSettings({ window: { width: "900", height: 700 } }).window).toBeNull();
    expect(parseSettings({ window: { width: Infinity, height: 700 } }).window).toBeNull();
  });
});

describe("SettingsStore", () => {
  it("starts from defaults when there is no file", () => {
    expect(new SettingsStore(dir()).get()).toEqual(defaultSettings());
  });

  it("starts from defaults when the file is not JSON", () => {
    const d = dir();
    writeFileSync(join(d, SETTINGS_FILE), "{ not json");
    expect(new SettingsStore(d).get()).toEqual(defaultSettings());
  });

  it("writes atomically, leaves no temporary file, and reads back what it wrote", () => {
    const d = dir();
    const store = new SettingsStore(d);
    store.update({ server: { url: "https://a.example/x", confirmedInsecure: false } });
    store.update({ window: { width: 1000, height: 800, maximized: true } });
    expect(readdirSync(d)).toEqual([SETTINGS_FILE]);
    const reread = new SettingsStore(d).get();
    expect(reread).toEqual({
      version: 1,
      server: { url: "https://a.example/", confirmedInsecure: false },
      window: { width: 1000, height: 800, maximized: true },
    });
    expect(JSON.parse(readFileSync(join(d, SETTINGS_FILE), "utf8"))).toEqual(reread);
  });

  it("creates the directory if it does not exist yet", () => {
    const d = join(dir(), "nested", "profile");
    new SettingsStore(d).update({ server: { url: "https://a.example", confirmedInsecure: false } });
    expect(new SettingsStore(d).get().server?.url).toBe("https://a.example/");
  });

  it("keeps the file private to the user on POSIX systems", () => {
    if (process.platform === "win32") return;
    const d = dir();
    new SettingsStore(d).update({ window: { width: 900, height: 700, maximized: false } });
    expect(statSync(join(d, SETTINGS_FILE)).mode & 0o077).toBe(0);
  });

  it("returns copies, so callers cannot change the store by accident", () => {
    const store = new SettingsStore(dir());
    store.update({ window: { width: 900, height: 700, maximized: false } });
    const copy = store.get();
    copy.window!.width = 1;
    expect(store.get().window?.width).toBe(900);
  });
});
