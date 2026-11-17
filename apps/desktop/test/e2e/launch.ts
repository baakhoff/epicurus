/**
 * Launching the app for the smoke — the same way for a development build (`electron .`) and a
 * packaged one (`EPICURUS_DESKTOP_EXECUTABLE`), driven over the Chrome DevTools Protocol.
 *
 * Why not Playwright's `_electron.launch`: it attaches through Node's `--inspect`, which the
 * packaged app refuses (the `EnableNodeCliInspectArguments` fuse is off). Chromium's
 * `--remote-debugging-port` still works, reaches every page, and is the same for both builds,
 * so the smoke that runs in `ci.yml` is exactly the smoke that runs on the packaged app.
 */
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { chromium, type Browser, type Page } from "@playwright/test";

const APP_DIR = resolve(__dirname, "..", "..");

export type RunningApp = {
  browser: Browser;
  process: ChildProcess;
  userDataDir: string;
  externalLog: string;
  /** The first page whose URL satisfies `match`, waiting for it to appear. */
  page(match: (url: string) => boolean, timeoutMs?: number): Promise<Page>;
  pages(): Page[];
  close(): Promise<void>;
};

export function newUserDataDir(): string {
  return mkdtempSync(join(tmpdir(), "epicurus-desktop-smoke-"));
}

export function command(): { file: string; args: string[] } {
  const executable = process.env.EPICURUS_DESKTOP_EXECUTABLE;
  const extra = (process.env.EPICURUS_SMOKE_EXTRA_ARGS ?? "").split(" ").filter(Boolean);
  if (executable) return { file: executable, args: extra };
  const electron = createRequire(join(APP_DIR, "package.json"))("electron") as unknown as string;
  return { file: electron, args: [APP_DIR, ...extra] };
}

export async function launchApp(userDataDir: string = newUserDataDir()): Promise<RunningApp> {
  mkdirSync(userDataDir, { recursive: true });
  const externalLog = join(userDataDir, "opened-externally.log");
  const { file, args } = command();
  const child = spawn(file, [...args, "--remote-debugging-port=0"], {
    env: {
      ...process.env,
      EPICURUS_DESKTOP_USER_DATA_DIR: userDataDir,
      EPICURUS_DESKTOP_OPEN_EXTERNAL_LOG: externalLog,
      ELECTRON_ENABLE_LOGGING: "1",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  const endpoint = await new Promise<string>((resolveEndpoint, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`no DevTools endpoint within 60 s; output:\n${output}`)),
      60_000,
    );
    const onData = (chunk: Buffer) => {
      output += chunk.toString();
      process.stderr.write(chunk);
      const match = /DevTools listening on (ws:\/\/\S+)/.exec(output);
      if (match?.[1]) {
        clearTimeout(timer);
        resolveEndpoint(match[1]);
      }
    };
    child.stdout?.on("data", onData);
    child.stderr?.on("data", onData);
    child.on("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`the app exited (code ${code}) before DevTools was up:\n${output}`));
    });
  });
  child.stdout?.on("data", (c: Buffer) => process.stderr.write(c));
  child.stderr?.on("data", (c: Buffer) => process.stderr.write(c));
  const browser = await chromium.connectOverCDP(endpoint);

  const pages = () => browser.contexts().flatMap((context) => context.pages());
  const page = async (match: (url: string) => boolean, timeoutMs = 30_000): Promise<Page> => {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const found = pages().find((p) => match(p.url()));
      if (found) return found;
      if (Date.now() > deadline) {
        throw new Error(`no page matched; open: ${pages().map((p) => p.url()).join(", ")}`);
      }
      await new Promise((r) => setTimeout(r, 200));
    }
  };

  const exited = new Promise<void>((r) => child.once("exit", () => r()));
  const close = async () => {
    if (child.exitCode !== null) return;
    try {
      const cdp = await browser.newBrowserCDPSession();
      await cdp.send("Browser.close");
    } catch {
      /* the connection drops as the browser closes */
    }
    const timeout = new Promise<"timeout">((r) => setTimeout(() => r("timeout"), 15_000));
    if ((await Promise.race([exited, timeout])) === "timeout") {
      if (process.platform === "win32" && child.pid !== undefined) {
        spawnSync("taskkill", ["/pid", String(child.pid), "/T", "/F"]);
      } else {
        child.kill("SIGKILL");
      }
      await exited;
    }
  };

  return { browser, process: child, userDataDir, externalLog, page, pages, close };
}

export function readSettings(userDataDir: string): Record<string, unknown> | null {
  const file = join(userDataDir, "settings.json");
  if (!existsSync(file)) return null;
  return JSON.parse(readFileSync(file, "utf8")) as Record<string, unknown>;
}

export function screenshotDir(): string {
  const dir = process.env.EPICURUS_SMOKE_SCREENSHOTS ?? join(APP_DIR, "test-results", "screenshots");
  mkdirSync(dir, { recursive: true });
  return dir;
}
