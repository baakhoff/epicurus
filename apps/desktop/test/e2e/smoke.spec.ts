/**
 * The launch smoke: the real app (development build or packaged), a stand-in server, and the
 * behaviour the window promises — connect, show the server, keep the session across a restart,
 * keep new windows out of the app, show which host it is on, and never be blank when the server
 * is down.
 */
import { spawn, spawnSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import { startFixture, type Fixture } from "./fixture-server";
import {
  command,
  launchApp,
  newUserDataDir,
  readSettings,
  screenshotDir,
  type RunningApp,
} from "./launch";

const isConnect = (url: string) => url.startsWith("epicurus-app://local/connect.html");
const isOffline = (url: string) => url.startsWith("epicurus-app://local/offline.html");
const isBar = (url: string) => url.startsWith("epicurus-app://local/bar.html");
const label = `${process.platform}-${process.arch}`;

async function shot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: join(screenshotDir(), `${label}-${name}.png`) });
}

/**
 * A screenshot of the whole screen, taken by the operating system — the native window, its title
 * bar and menu, not only the page. Only when EPICURUS_SMOKE_DESKTOP_SCREENSHOT=1 (CI), and never
 * a failure: a runner without screen-capture rights still has the page screenshots.
 */
function desktopShot(name: string): void {
  if (process.env.EPICURUS_SMOKE_DESKTOP_SCREENSHOT !== "1") return;
  const file = join(screenshotDir(), `${label}-${name}-desktop.png`);
  const run =
    process.platform === "darwin"
      ? spawnSync("screencapture", ["-x", file])
      : process.platform === "win32"
        ? spawnSync("powershell", [
            "-NoProfile",
            "-Command",
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;" +
              "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;" +
              "$i=New-Object System.Drawing.Bitmap $b.Width,$b.Height;" +
              "$g=[System.Drawing.Graphics]::FromImage($i);" +
              "$g.CopyFromScreen($b.Left,$b.Top,0,0,$i.Size);" +
              `$i.Save('${file.replace(/'/g, "''")}')`,
          ])
        : spawnSync("import", ["-window", "root", file]);
  console.log(`desktop screenshot ${file}: exit ${run.status} ${run.error ?? ""} ${run.stderr ?? ""}`);
}

test.describe.configure({ mode: "serial" });

let server: Fixture;
let elsewhere: Fixture;
let app: RunningApp | null = null;
const userDataDir = newUserDataDir();

test.beforeAll(async () => {
  elsewhere = await startFixture();
  server = await startFixture({ elsewhere: elsewhere.origin });
});

test.afterAll(async () => {
  await app?.close();
  await server?.close();
  await elsewhere?.close();
});

test("first run shows the connect page, checks the address and connects", async () => {
  app = await launchApp(userDataDir);
  const connect = await app.page(isConnect);
  await expect(connect.locator("h1")).toHaveText("Connect to a server");
  await shot(connect, "1-connect");

  // The local page has the shell API; nothing has been saved yet.
  expect(await connect.evaluate(() => typeof window.epicurusShell)).toBe("object");
  expect(readSettings(userDataDir)?.server ?? null).toBeNull();

  // A bare host:port on loopback is normalised to http:// and checked against /platform/v1/info.
  await connect.fill("#address", `127.0.0.1:${server.port}/some/deep/link`);
  await connect.click("#submit");
  await connect.waitForURL(`${server.origin}/`);
  await expect(connect.locator("#hello")).toHaveText("epicurus fixture server");
  await expect(connect.locator("#session")).toHaveText("session: new");
  expect(server.requests).toContain("GET /platform/v1/info");
  expect(readSettings(userDataDir)?.server).toEqual({
    url: `${server.origin}/`,
    confirmedInsecure: false,
  });
  await shot(connect, "2-server");
});

test("the server page gets the read-only desktop marker and no shell API", async () => {
  const page = await app!.page((u) => u.startsWith(server.origin));
  const marker = await page.evaluate(() => {
    const desktop = (window as unknown as { epicurusDesktop?: { capabilities: { webPush: boolean } } })
      .epicurusDesktop;
    return {
      webPush: desktop?.capabilities.webPush,
      shell: typeof (window as unknown as { epicurusShell?: unknown }).epicurusShell,
      node: typeof (window as unknown as { require?: unknown }).require,
      process: typeof (window as unknown as { process?: unknown }).process,
    };
  });
  expect(marker).toEqual({ webPush: false, shell: "undefined", node: "undefined", process: "undefined" });
});

test("web push cannot work in the window: permission is refused", async () => {
  const page = await app!.page((u) => u.startsWith(server.origin));
  const probe = await page.evaluate(async () => {
    const result: Record<string, string> = {
      pushManager: String("PushManager" in window),
      serviceWorker: String("serviceWorker" in navigator),
    };
    result.permission = await Notification.requestPermission();
    try {
      const registration = await navigator.serviceWorker.register("/sw.js");
      await navigator.serviceWorker.ready;
      const key = new Uint8Array(65);
      key[0] = 4;
      await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
      result.subscribe = "subscribed";
    } catch (error) {
      result.subscribe = String(error);
    }
    return result;
  });
  // Recorded in the run log: what Electron does with web push on this platform.
  console.log(`web push probe (${label}): ${JSON.stringify(probe)}`);
  writeFileSync(join(screenshotDir(), `${label}-push-probe.json`), JSON.stringify(probe, null, 2));
  expect(probe.permission).toBe("denied");
  expect(probe.subscribe).not.toBe("subscribed");
});

test("a new-window link opens in the system browser, not in the app", async () => {
  const page = await app!.page((u) => u.startsWith(server.origin));
  const before = app!.pages().length;
  await page.click("#new-window");
  await expect
    .poll(() => {
      try {
        return readFileSync(app!.externalLog, "utf8");
      } catch {
        return "";
      }
    })
    .toContain("https://example.invalid/docs");
  await page.waitForTimeout(500);
  expect(app!.pages().length).toBe(before);
  expect(page.url()).toBe(`${server.origin}/`);
});

test("a file: link is refused", async () => {
  const page = await app!.page((u) => u.startsWith(server.origin));
  await page.click("#file-link");
  await page.waitForTimeout(500);
  expect(page.url()).toBe(`${server.origin}/`);
});

test("off the server, a bar names the host and leads back", async () => {
  const page = await app!.page((u) => u.startsWith(server.origin));
  await page.click("#elsewhere");
  await page.waitForURL(`${elsewhere.origin}/sign-in`);
  const bar = await app!.page(isBar);
  await expect(bar.locator("#host")).toHaveText(`127.0.0.1:${elsewhere.port}`);
  await expect(bar.locator("#scheme-note")).toHaveText(" (not encrypted)");
  await shot(bar, "3-host-bar");
  await bar.click("#back");
  await page.waitForURL(`${server.origin}/`);
  await expect(page.locator("#hello")).toBeVisible();
});

test("after a restart the app is still connected, with its session", async () => {
  await app!.close();
  app = await launchApp(userDataDir);
  const page = await app.page((u) => u.startsWith(server.origin));
  await expect(page.locator("#session")).toHaveText("session: restored");
  // The window state is recorded (the restore logic itself is unit-tested in windowState.test.ts;
  // the DevTools protocol cannot resize an Electron window to prove a round trip here).
  const settings = readSettings(userDataDir);
  // `maximized` is whatever the platform reports: macOS calls a window that fills a small
  // runner screen "maximized" (zoomed).
  const saved = settings?.window as { width: number; height: number; maximized: unknown };
  expect(saved.width).toBeGreaterThanOrEqual(480);
  expect(saved.height).toBeGreaterThanOrEqual(360);
  expect(typeof saved.maximized).toBe("boolean");
});

test("an unreachable server shows Retry and Change server, and Retry recovers", async () => {
  await app!.close();
  const port = server.port;
  await server.close();
  app = await launchApp(userDataDir);
  const offline = await app.page(isOffline);
  await expect(offline.locator("#title")).toHaveText("Server not reachable");
  await expect(offline.locator("#retry")).toBeVisible();
  await expect(offline.locator("#change")).toBeVisible();
  await shot(offline, "4-offline");

  server = await startFixture({ port, elsewhere: elsewhere.origin });
  await offline.click("#retry");
  await offline.waitForURL(`${server.origin}/`);
  await expect(offline.locator("#hello")).toBeVisible();
});

test("Change server refuses an address that is not an epicurus server", async () => {
  const page = await app!.page((u) => u.startsWith(server.origin));
  const other = await startFixture({ kind: "other" });
  try {
    await expect(page.locator("#hello")).toBeVisible();
    await app!.close();
    // Point the saved server at a closed port so the offline page (and its button) shows.
    const dead = await startFixture();
    const deadUrl = `${dead.origin}/`;
    await dead.close();
    const settingsFile = join(userDataDir, "settings.json");
    const saved = JSON.parse(readFileSync(settingsFile, "utf8")) as Record<string, unknown>;
    writeFileSync(settingsFile, JSON.stringify({ ...saved, server: { url: deadUrl } }));
    app = await launchApp(userDataDir);
    // The offline page's Change server button makes the same call as the menu item.
    const offline = await app.page(isOffline);
    await offline.click("#change");
    const connect = await app.page(isConnect);
    await expect(connect.locator("#address")).toHaveValue(deadUrl);
    await expect(connect.locator("#cancel")).toBeVisible();

    await connect.fill("#address", other.origin);
    await connect.click("#submit");
    await expect(connect.locator("#status")).toContainText("not an epicurus server");

    await connect.fill("#address", "ftp://example.com");
    await connect.click("#submit");
    await expect(connect.locator("#status")).toContainText("Only http:// and https://");

    await connect.fill("#address", dead.origin);
    await connect.click("#submit");
    await expect(connect.locator("#status")).toContainText("Could not reach");
    await shot(connect, "5-connect-error");

    // Nothing failed was saved.
    expect(readSettings(userDataDir)?.server).toMatchObject({ url: deadUrl });
  } finally {
    await other.close();
  }
});

test("the real web UI renders in the window", async () => {
  const dist = process.env.EPICURUS_WEB_DIST;
  test.skip(!dist, "EPICURUS_WEB_DIST is not set (a built services/web/dist)");
  await app?.close();
  app = null;
  const web = await startFixture({ kind: "web", webDist: dist! });
  const dir = newUserDataDir();
  writeFileSync(
    join(dir, "settings.json"),
    JSON.stringify({ version: 1, server: { url: `${web.origin}/`, confirmedInsecure: false } }),
  );
  try {
    app = await launchApp(dir);
    const page = await app.page((u) => u.startsWith(web.origin));
    await expect(page.locator("#root")).not.toBeEmpty();
    await page.waitForTimeout(3_000);
    await shot(page, "6-web-ui");
    desktopShot("6-web-ui");
    await page.goto(`${web.origin}/settings`);
    const push = page.getByText("Push notifications", { exact: true });
    await push.scrollIntoViewIfNeeded();
    await expect(page.getByText("available in the desktop app", { exact: false })).toBeVisible();
    await shot(page, "7-web-ui-settings-push");
  } finally {
    await app?.close();
    app = null;
    await web.close();
  }
});

declare global {
  interface Window {
    epicurusShell: unknown;
  }
}

test("the app refuses a DevTools port onto its default profile", async () => {
  // Without EPICURUS_DESKTOP_USER_DATA_DIR the profile is the user's real one: the app must exit
  // before Chromium opens the port (src/main/debugging.ts).
  const { file, args } = command();
  // Logging on, as launchApp does, so Chromium's "DevTools listening" line would show if it ran.
  const env: NodeJS.ProcessEnv = { ...process.env, ELECTRON_ENABLE_LOGGING: "1" };
  delete env.EPICURUS_DESKTOP_USER_DATA_DIR;
  const child = spawn(file, [...args, "--remote-debugging-port=0"], {
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  child.stdout?.on("data", (c: Buffer) => (output += c.toString()));
  child.stderr?.on("data", (c: Buffer) => (output += c.toString()));
  const code = await new Promise<number | null>((resolveExit) => {
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      resolveExit(null);
    }, 30_000);
    child.once("exit", (exitCode) => {
      clearTimeout(timer);
      resolveExit(exitCode);
    });
  });
  expect(output).not.toContain("DevTools listening");
  // The exit code is the proof on every platform; the message is checked where stderr of a GUI
  // build is known to reach the pipe.
  if (process.platform !== "win32") expect(output).toContain("refuses --remote-debugging-port");
  expect(code).toBe(1);
});
