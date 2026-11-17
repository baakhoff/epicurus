/**
 * The main process: one window around the web UI of the server the user connected to.
 *
 * Flow: no saved server → the local "Connect to a server" page; a saved server → its web UI, in a
 * persistent session (`persist:epicurus`) so a signed-in session survives a restart. A load that
 * fails, or a 502/503/504 from the server, replaces the page with a local "not reachable" page
 * (Retry / Change server) — the window is never left blank.
 *
 * Security choices are documented in docs/apps/desktop.md and at each seam: `navigation.ts`,
 * `permissions.ts`, `appAssets.ts`, the preload, and the fuses in `electron-builder.yml`.
 */
import * as fs from "node:fs";
import * as path from "node:path";

import {
  app,
  BaseWindow,
  Menu,
  WebContentsView,
  ipcMain,
  protocol,
  screen,
  session,
  shell,
  webContents as allWebContents,
  type IpcMainInvokeEvent,
  type Session,
  type WebPreferences,
} from "electron";

import { needsInsecureConfirmation, normaliseServerAddress } from "./address";
import { APP_CSP, resolveAppAsset } from "./appAssets";
import { CHANNELS } from "./channels";
import { buildMenuTemplate, type EditCommand } from "./menu";
import {
  APP_ORIGIN,
  APP_SCHEME,
  decideNavigation,
  decideWindowOpen,
  isAppPage,
  isOnServer,
  offOriginHost,
} from "./navigation";
import { decidePermission } from "./permissions";
import { mustRefuseRemoteDebugging } from "./debugging";
import { checkServer, describeLoadFailure, type FetchLike } from "./serverCheck";
import { SettingsStore } from "./settings";
import { restoreBounds } from "./windowState";

const APP_NAME = "Epicurus";
const PARTITION = "persist:epicurus";
const BAR_HEIGHT = 40;
/** Chromium's "the load was replaced by another one" — not a failure. */
const ERR_ABORTED = -3;
const UNAVAILABLE_STATUSES = new Set([502, 503, 504]);

// ── test and portable-install seams (documented in docs/apps/desktop.md) ─────────────────────
const userDataOverride = process.env.EPICURUS_DESKTOP_USER_DATA_DIR;
// Before anything else: no DevTools port onto the user's real profile (src/main/debugging.ts).
if (
  mustRefuseRemoteDebugging(
    (name) => app.commandLine.hasSwitch(name),
    userDataOverride,
    app.getPath("userData"),
  )
) {
  console.error(
    "Epicurus refuses --remote-debugging-port / --remote-debugging-pipe on its default profile; " +
      "set EPICURUS_DESKTOP_USER_DATA_DIR to a separate directory to debug.",
  );
  app.exit(1);
  process.exit(1);
}
if (userDataOverride) app.setPath("userData", path.resolve(userDataOverride));
const externalLog = process.env.EPICURUS_DESKTOP_OPEN_EXTERNAL_LOG;

app.setName(APP_NAME);
app.enableSandbox();
protocol.registerSchemesAsPrivileged([
  { scheme: APP_SCHEME, privileges: { standard: true, secure: true } },
]);

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  void app.whenReady().then(start);
}

// ── state ────────────────────────────────────────────────────────────────────────────────────
let settings: SettingsStore;
let ses: Session;
let win: BaseWindow | null = null;
let page: WebContentsView | null = null;
let bar: WebContentsView | null = null;
let barHost: string | null = null;
/** The last address the connect page checked successfully — the only one `connect` accepts. */
let lastChecked: { url: string; insecureNeedsConfirm: boolean } | null = null;
let saveTimer: NodeJS.Timeout | null = null;

function serverUrl(): string | null {
  return settings.get().server?.url ?? null;
}

function serverOrigin(): string | null {
  const url = serverUrl();
  return url ? new URL(url).origin : null;
}

function rendererRoot(): string {
  return path.join(__dirname, "..", "renderer");
}

function appPage(name: string, params: Record<string, string> = {}): string {
  const query = new URLSearchParams(params).toString();
  return `${APP_ORIGIN}/${name}${query ? `?${query}` : ""}`;
}

function openExternal(url: string): void {
  if (decideWindowOpen(url) !== "open-external") return;
  if (externalLog) {
    fs.appendFileSync(externalLog, `${url}\n`);
    return;
  }
  void shell.openExternal(url);
}

// ── pages ────────────────────────────────────────────────────────────────────────────────────
function loadServer(): void {
  const url = serverUrl();
  if (!page) return;
  if (!url) {
    showConnect();
    return;
  }
  void page.webContents.loadURL(url).catch(() => {
    /* did-fail-load shows the offline page */
  });
}

function showConnect(): void {
  if (!page) return;
  const current = serverUrl();
  const params: Record<string, string> = {};
  if (current) {
    params.address = current;
    params.cancel = "1";
  }
  setBar(null);
  void page.webContents.loadURL(appPage("connect.html", params));
}

function showOffline(kind: string, message: string): void {
  if (!page) return;
  setBar(null);
  void page.webContents.loadURL(
    appPage("offline.html", { kind, message, server: serverUrl() ?? "" }),
  );
}

function setBar(host: string | null, insecure = false): void {
  if (!win || !bar) return;
  if (host === barHost) return;
  barHost = host;
  if (host === null) {
    win.contentView.removeChildView(bar);
  } else {
    win.contentView.addChildView(bar);
    void bar.webContents.loadURL(
      appPage("bar.html", { host, insecure: insecure ? "1" : "0" }),
    );
  }
  layout();
}

function layout(): void {
  if (!win || !page) return;
  const { width, height } = win.getContentBounds();
  const top = barHost === null ? 0 : BAR_HEIGHT;
  bar?.setBounds({ x: 0, y: 0, width, height: top });
  page.setBounds({ x: 0, y: top, width, height: Math.max(0, height - top) });
}

function updateChrome(url: string): void {
  if (!win) return;
  const host = offOriginHost(url, serverOrigin());
  setBar(host, host !== null && url.startsWith("http:"));
  const title = page?.webContents.getTitle().trim() ?? "";
  win.setTitle(host ? `${host} — ${APP_NAME}` : title || APP_NAME);
}

// ── the window ───────────────────────────────────────────────────────────────────────────────
function webPreferences(): WebPreferences {
  return {
    session: ses,
    preload: path.join(__dirname, "..", "preload", "preload.js"),
    contextIsolation: true,
    sandbox: true,
    nodeIntegration: false,
    nodeIntegrationInWorker: false,
    nodeIntegrationInSubFrames: false,
    webSecurity: true,
    allowRunningInsecureContent: false,
    webviewTag: false,
    navigateOnDragDrop: false,
    safeDialogs: true,
    spellcheck: true,
  };
}

function scheduleSaveWindow(): void {
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(saveWindow, 500);
}

function saveWindow(): void {
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = null;
  if (!win || win.isDestroyed()) return;
  const normal = win.getNormalBounds();
  settings.update({ window: { ...normal, maximized: win.isMaximized() } });
}

function createWindow(): void {
  const bounds = restoreBounds(
    settings.get().window,
    screen.getAllDisplays().map((d) => d.workArea),
  );
  win = new BaseWindow({
    x: bounds.x,
    y: bounds.y,
    width: bounds.width,
    height: bounds.height,
    minWidth: 480,
    minHeight: 360,
    title: APP_NAME,
    backgroundColor: "#121411",
    show: false,
  });
  page = new WebContentsView({ webPreferences: webPreferences() });
  bar = new WebContentsView({ webPreferences: webPreferences() });
  page.setBackgroundColor("#121411");
  bar.setBackgroundColor("#1a1d17");
  win.contentView.addChildView(page);
  // Load the bar now, hidden: it is ready the moment a page leaves the server.
  void bar.webContents.loadURL(appPage("bar.html"));
  barHost = null;
  layout();
  if (bounds.maximized) win.maximize();
  win.show();
  page.webContents.focus();
  // Record the opening bounds too: a window that is never moved or resized, and is closed by a
  // path that skips the close event, still comes back where it opened.
  scheduleSaveWindow();

  win.on("resize", () => {
    layout();
    scheduleSaveWindow();
  });
  win.on("move", scheduleSaveWindow);
  win.on("close", saveWindow);
  win.on("closed", () => {
    win = null;
    page = null;
    bar = null;
    barHost = null;
  });

  const contents = page.webContents;
  contents.on("will-navigate", (event) => {
    if (decideNavigation(event.url) === "refuse") event.preventDefault();
  });
  contents.on("will-redirect", (event) => {
    if (event.isMainFrame && decideNavigation(event.url) === "refuse") event.preventDefault();
  });
  contents.on("did-navigate", (_event, url, httpResponseCode) => {
    if (isOnServer(url, serverOrigin()) && UNAVAILABLE_STATUSES.has(httpResponseCode)) {
      showOffline(
        "unreachable",
        `${new URL(url).host} answered HTTP ${httpResponseCode}: the server behind it is not running.`,
      );
      return;
    }
    updateChrome(url);
  });
  contents.on("did-navigate-in-page", (_event, url, isMainFrame) => {
    if (isMainFrame) updateChrome(url);
  });
  contents.on("page-title-updated", () => updateChrome(contents.getURL()));
  contents.on("did-finish-load", () => {
    // Write cookies now, not on Chromium's 30-second timer: a sign-in survives even a crash.
    if (isOnServer(contents.getURL(), serverOrigin())) void ses.cookies.flushStore();
  });
  contents.on("did-fail-load", (_event, errorCode, errorDescription, validatedURL, isMainFrame) => {
    if (!isMainFrame || errorCode === ERR_ABORTED || isAppPage(validatedURL)) return;
    let host = validatedURL;
    try {
      host = new URL(validatedURL).host;
    } catch {
      /* keep the raw URL */
    }
    const failure = describeLoadFailure(errorCode, errorDescription, host);
    showOffline(failure.kind, failure.message);
  });
  contents.on("render-process-gone", (_event, details) => {
    if (details.reason === "clean-exit") return;
    showOffline("crashed", "The page stopped working. Retry to load it again.");
  });

  if (serverUrl()) loadServer();
  else showConnect();
}

// ── IPC from the local pages ─────────────────────────────────────────────────────────────────
function fromAppPage(event: IpcMainInvokeEvent): boolean {
  const frame = event.senderFrame;
  return frame !== null && frame.parent === null && isAppPage(frame.url);
}

function handle<A extends unknown[], R>(
  channel: string,
  fn: (...args: A) => R | Promise<R>,
): void {
  ipcMain.handle(channel, async (event, ...args) => {
    if (!fromAppPage(event)) throw new Error("refused: not an app page");
    return fn(...(args as A));
  });
}

const sessionFetch: FetchLike = async (url, init) => {
  const response = await ses.fetch(url, { ...init, bypassCustomProtocolHandlers: true });
  return response;
};

function registerIpc(): void {
  handle(CHANNELS.checkServer, async (input: unknown) => {
    lastChecked = null;
    const address = normaliseServerAddress(typeof input === "string" ? input : "");
    if (!address.ok) return { ok: false, kind: "invalid", message: address.error };
    const result = await checkServer(address.origin, sessionFetch);
    if (!result.ok) return result;
    const insecureNeedsConfirm = needsInsecureConfirmation(address);
    lastChecked = { url: address.url, insecureNeedsConfirm };
    return {
      ok: true,
      url: address.url,
      host: address.host,
      version: result.info.version,
      signInRequired: result.info.signInRequired,
      needsInsecureConfirmation: insecureNeedsConfirm,
    };
  });

  handle(CHANNELS.connect, (url: unknown, confirmedInsecure: unknown) => {
    if (!lastChecked || lastChecked.url !== url) {
      return { ok: false, message: "Check the address first." };
    }
    if (lastChecked.insecureNeedsConfirm && confirmedInsecure !== true) {
      return { ok: false, message: "Confirm the unencrypted connection first." };
    }
    settings.update({
      server: { url: lastChecked.url, confirmedInsecure: lastChecked.insecureNeedsConfirm },
    });
    lastChecked = null;
    loadServer();
    return { ok: true };
  });

  handle(CHANNELS.cancelConnect, () => {
    if (serverUrl()) loadServer();
  });
  handle(CHANNELS.retry, () => loadServer());
  handle(CHANNELS.changeServer, () => showConnect());
  handle(CHANNELS.backToServer, () => loadServer());
}

// ── menu ─────────────────────────────────────────────────────────────────────────────────────
function installMenu(): void {
  const contents = () => page?.webContents ?? null;
  const template = buildMenuTemplate(process.platform, APP_NAME, {
    changeServer: () => showConnect(),
    reload: () => contents()?.reload(),
    forceReload: () => contents()?.reloadIgnoringCache(),
    toggleDevTools: () => contents()?.toggleDevTools(),
    zoom: (step) => {
      const wc = contents();
      if (!wc) return;
      wc.setZoomLevel(step === 0 ? 0 : Math.min(5, Math.max(-5, wc.getZoomLevel() + step * 0.5)));
    },
    toggleFullScreen: () => win?.setFullScreen(!win.isFullScreen()),
    minimize: () => win?.minimize(),
    toggleMaximize: () => (win?.isMaximized() ? win.unmaximize() : win?.maximize()),
    closeWindow: () => win?.close(),
    quit: () => app.quit(),
    edit: (command: EditCommand) => {
      const wc = allWebContents.getFocusedWebContents() ?? contents();
      wc?.[command]();
    },
    openExternal,
  });
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ── start ────────────────────────────────────────────────────────────────────────────────────
function start(): void {
  settings = new SettingsStore(app.getPath("userData"));
  ses = session.fromPartition(PARTITION);

  // Look like the Chromium it is: some identity providers refuse a sign-in page to a user agent
  // that names Electron.
  ses.setUserAgent(ses.getUserAgent().replace(/\s(Electron|Epicurus|epicurus-desktop)\/\S+/g, ""));

  ses.protocol.handle(APP_SCHEME, (request) => {
    const asset = resolveAppAsset(request.url, rendererRoot());
    if (!asset) return new Response("Not found", { status: 404 });
    return new Response(fs.readFileSync(asset.file), {
      headers: {
        "content-type": asset.contentType,
        "content-security-policy": APP_CSP,
        "x-content-type-options": "nosniff",
      },
    });
  });

  ses.setPermissionRequestHandler((wc, permission, callback, details) => {
    callback(decidePermission(permission, details.requestingUrl || wc.getURL(), serverOrigin()));
  });
  ses.setPermissionCheckHandler((_wc, permission, requestingOrigin) =>
    decidePermission(permission, requestingOrigin, serverOrigin()),
  );
  ses.setDevicePermissionHandler(() => false);

  registerIpc();
  installMenu();
  createWindow();
}

app.on("web-contents-created", (_event, contents) => {
  contents.on("will-attach-webview", (event) => event.preventDefault());
  contents.setWindowOpenHandler(({ url }) => {
    openExternal(url);
    return { action: "deny" };
  });
  contents.on("will-frame-navigate", (event) => {
    if (event.isMainFrame) return;
    const target = event.url;
    if (target.startsWith(`${APP_SCHEME}:`) || target.startsWith("file:")) event.preventDefault();
  });
});

app.on("second-instance", () => {
  if (!app.isReady() || !settings) return;
  if (!win) {
    createWindow();
    return;
  }
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
});

app.on("activate", () => {
  if (!win && app.isReady()) createWindow();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  if (win && !win.isDestroyed()) saveWindow();
  if (ses) void ses.cookies.flushStore();
});

process.on("SIGTERM", () => app.quit());
