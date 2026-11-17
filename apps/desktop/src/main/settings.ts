/**
 * The settings store — one small JSON file, `settings.json`, in the app's user-data directory.
 *
 * It holds only what the app needs to come back the way it was left: the server address and
 * the window's size and position. No secrets: the session is a cookie in the browser profile
 * beside it, managed by Chromium.
 *
 * Reads are forgiving (a missing, unreadable or hand-edited file falls back field by field to
 * the defaults, never crashes the app); writes are atomic (a temporary file renamed over the old
 * one, so a crash mid-write cannot leave half a file).
 */
import * as fs from "node:fs";
import * as path from "node:path";

import { normaliseServerAddress } from "./address";

export const SETTINGS_FILE = "settings.json";
export const SETTINGS_VERSION = 1;

export type ServerSetting = {
  /** The normalised address (`scheme://host[:port]/`). */
  url: string;
  /** The user confirmed plain HTTP to a non-loopback host. */
  confirmedInsecure: boolean;
};

export type WindowState = {
  x?: number;
  y?: number;
  width: number;
  height: number;
  maximized: boolean;
};

export type Settings = {
  version: number;
  server: ServerSetting | null;
  window: WindowState | null;
};

export const DEFAULT_WINDOW: WindowState = { width: 1280, height: 860, maximized: false };
export const MIN_WIDTH = 480;
export const MIN_HEIGHT = 360;

export function defaultSettings(): Settings {
  return { version: SETTINGS_VERSION, server: null, window: null };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function finiteInt(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? Math.round(value) : undefined;
}

function parseServer(value: unknown): ServerSetting | null {
  if (!isRecord(value) || typeof value.url !== "string") return null;
  const address = normaliseServerAddress(value.url);
  if (!address.ok) return null;
  return { url: address.url, confirmedInsecure: value.confirmedInsecure === true };
}

function parseWindow(value: unknown): WindowState | null {
  if (!isRecord(value)) return null;
  const width = finiteInt(value.width);
  const height = finiteInt(value.height);
  if (width === undefined || height === undefined) return null;
  const state: WindowState = {
    width: Math.max(MIN_WIDTH, width),
    height: Math.max(MIN_HEIGHT, height),
    maximized: value.maximized === true,
  };
  const x = finiteInt(value.x);
  const y = finiteInt(value.y);
  if (x !== undefined && y !== undefined) {
    state.x = x;
    state.y = y;
  }
  return state;
}

/** Settings from whatever JSON value was on disk — each field validated on its own. */
export function parseSettings(raw: unknown): Settings {
  if (!isRecord(raw)) return defaultSettings();
  return {
    version: SETTINGS_VERSION,
    server: parseServer(raw.server),
    window: parseWindow(raw.window),
  };
}

export class SettingsStore {
  readonly file: string;
  private current: Settings;

  constructor(directory: string) {
    this.file = path.join(directory, SETTINGS_FILE);
    this.current = this.read();
  }

  private read(): Settings {
    let text: string;
    try {
      text = fs.readFileSync(this.file, "utf8");
    } catch {
      return defaultSettings();
    }
    try {
      return parseSettings(JSON.parse(text));
    } catch {
      return defaultSettings();
    }
  }

  get(): Settings {
    return structuredClone(this.current);
  }

  /** Apply `patch`, re-validate the whole, and write it to disk atomically. */
  update(patch: Partial<Omit<Settings, "version">>): Settings {
    this.current = parseSettings({ ...this.current, ...patch });
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const temporary = `${this.file}.${process.pid}.tmp`;
    fs.writeFileSync(temporary, `${JSON.stringify(this.current, null, 2)}\n`, { mode: 0o600 });
    fs.renameSync(temporary, this.file);
    return this.get();
  }
}
