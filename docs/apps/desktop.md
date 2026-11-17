# desktop — the app shell

**What it is.** `apps/desktop` is the epicurus desktop app for macOS, Windows and Linux: an
[Electron](https://www.electronjs.org/) window around the **web UI of a server the user connects
to** (#1015). It bundles no copy of the UI — the [web shell](../services/web.md) calls the API
with root-relative `/platform/...` paths, so it only works loaded from the server's own origin —
and it runs no local service yet. What it adds over a browser tab: a "Connect to a server" first
run, a window that remembers its server, session, size and position, the standard menus, a
visible host bar whenever a sign-in flow takes the window to another site, a local page instead
of a blank window when the server is down, and a hardened engine (sandboxed, no Node in the page,
permissions denied by default). Electron was chosen over Tauri because Chromium is the one engine
the project can test on Linux and in CI before anyone on it has a Mac, and it keeps the shell in
TypeScript like the web UI. Version **0.1.0**. Installing it as a user:
[the desktop app](../user/desktop-app.md).

## The contract

The app has no HTTP endpoint, MCP tool or NATS event of its own. It talks to a server through
two existing endpoints and to the web UI through one read-only object.

### What it asks of a server

| Call | When | What the app makes of it |
| --- | --- | --- |
| `GET /platform/v1/info` | the connect page's check | **200** with `contract_version`, `core_version` and `tenant` strings → an epicurus server; `core_app_version` (or `core_version` on an older core) is shown. **401** `{"code": "unauthenticated"}` → sign-in is on (`AUTH_MODE=oidc`); confirmed with the call below. **3xx** → "connect to *that origin* instead". **502/503/504** → not reachable. Anything else → "not an epicurus server". |
| `GET /platform/v1/auth/session` | only after a 401 above | **200** with `mode` (string) and `signed_in` (boolean) → an epicurus server with sign-in on. It is exempt from sign-in, which is why it can confirm what `/info` cannot. |
| `GET /` (the web UI) | every start, Retry, Back to Epicurus | loaded in the window. A load failure or a 502/503/504 shows the offline page. |

The check runs in the main process through the window's own Chromium network stack
(`session.fetch`), with redirects not followed and a 10-second timeout, so proxies and the
operating system's certificate store apply exactly as they will for the window. Failures are
explained as: *not reachable*, *no server with that name*, *did not answer in time*, *certificate
not trusted*, or *not an epicurus server*.

### What it gives the web UI: `window.epicurusDesktop`

Every web page in the window gets one frozen, read-only object from the preload:

```ts
window.epicurusDesktop = {
  shell: "electron",
  platform: "darwin" | "win32" | "linux",
  capabilities: { webPush: false },
};
```

The web UI uses it in one place: `isDesktopAppWithoutPush()` in `services/web/src/lib/push.ts`.
When `capabilities.webPush === false`, `isPushSupported()` answers `false` and **Settings → Push
notifications** says push is not available in the desktop app instead of offering a Subscribe
button that cannot work (the rest of the card — other devices, categories, quiet hours, the test
button — stays). Why it cannot work, measured by the launch smoke on every platform: Electron's
Chromium exposes `PushManager`, but there is no push service behind it —
`pushManager.subscribe()` rejects with `AbortError: Registration failed - push service not
available` — and the app refuses the `notifications` permission. A future shell that can deliver
notifications flips the capability; the web UI needs no change. The object is exposed to every
`http(s)` page in the window, including an identity provider's sign-in page (the preload cannot
know the server's origin without a synchronous IPC call); it says only that this is the epicurus
desktop app on a given platform. The user agent does **not** say so (the app strips `Electron/…`
from it), so any site the window visits can tell it is in the app from this object alone — accepted
for now; nothing in it is secret. Any page can also define the object itself, which only hides its
own push card.

### The local pages

Three pages ship inside the app and are served from `epicurus-app://local/` (a privileged,
standard, secure custom scheme with a fixed list of files and a strict Content-Security-Policy —
`default-src 'none'; script-src 'self'; style-src 'self'`): `connect.html`, `offline.html` and
`bar.html`. They are the only pages that get `window.epicurusShell`, the six calls they need
(check an address, connect, cancel, retry, change server, back to the server) over IPC; the main
process refuses those calls from anything but a top-level frame on `epicurus-app://local/`.
`connect` only accepts the address the last successful check returned, and plain `http://` to a
host that is not this machine only with the user's explicit confirmation.

## Behaviour

- **First run.** No saved server → the connect page. The address is normalised
  (`src/main/address.ts`): whitespace trimmed; without a scheme `https://` is assumed, or
  `http://` for a loopback host (`localhost`, `*.localhost`, `127.0.0.0/8`, `::1`); only
  `http:`/`https:`; credentials refused; path, query and fragment dropped — the result is
  `scheme://host[:port]/`. Plain `http://` to any other host (a LAN address included) shows a
  warning that must be confirmed before anything is saved.
- **The main window** loads the saved server in the persistent session partition
  `persist:epicurus`; cookies are flushed to disk after every page load on the server, so a
  signed-in session survives a restart (and a crash).
- **Navigation policy** (`src/main/navigation.ts`). Top-level navigation started by a page to any
  `http(s)` address is allowed — signing in (`/platform/v1/auth/login` → the identity provider →
  back) and connecting an account (`window.location.href = auth_url`) are full-page round trips.
  Every other scheme (`file:`, `data:`, `javascript:`, the app's own scheme, custom schemes) is
  refused, for navigations and redirects alike; sub-frames may not load `file:` or the app's
  scheme. **New windows** (`target="_blank"`, `window.open`) never open in the app: `http(s)` and
  `mailto:` go to the system's default handler, anything else is dropped. `<webview>` is refused.
- **Host bar.** Whenever the page is not on the server's origin, a 40-pixel bar above it names the
  host (and says "not encrypted" for `http:`) with a **Back to Epicurus** button; the window title
  becomes `<host> — Epicurus`.
- **Server unreachable** — at start, on Retry, on any navigation, or a 502/503/504 from the server
  — the page is replaced by the offline page (*not reachable*, *not answering*, *certificate not
  trusted*, or *the page stopped working* after a renderer crash) with **Retry** and **Change
  server**. While the web UI is loaded and the server goes away, the web UI's own
  [backend-unreachable banner](../services/web.md#offline--backend-unreachable-banner-494-debounced-791)
  takes over; the offline page appears on the next reload or navigation.
- **Menus** (`src/main/menu.ts`): the macOS app menu (About, **Change Server…**, Services, Hide,
  Quit) or File (**Change Server…**, Exit); Edit (undo, redo, cut, copy, paste, select all — native
  roles on macOS, where copy and paste only work through the menu); View (reload, force reload,
  developer tools, actual size, zoom in/out, full screen); Window (minimise, zoom/maximise, close);
  Help (this documentation, the project). The window is a `BaseWindow` holding the page and the bar
  as two views, so View and Window act on the page through explicit handlers, not menu roles.
- **One instance.** A second launch focuses the running window.
- **Window state.** Size, position and maximised state are saved (debounced) and restored; a
  position that would land off every current display is dropped and the window is centred.

## Security choices

| Choice | Where | Why |
| --- | --- | --- |
| `contextIsolation`, `sandbox`, no `nodeIntegration` (also not in workers or sub-frames), `webSecurity` on, no insecure content, no `<webview>` | `webPreferences()` in `src/main/main.ts`; `app.enableSandbox()` | the server's page is remote content; it gets nothing a browser tab would not. An ESLint rule refuses `nodeIntegration: true`, `contextIsolation: false` and `sandbox: false`. |
| A minimal preload | `src/preload/preload.ts` | one frozen marker object for web pages; the IPC calls only on the app's own pages, never `ipcRenderer` itself. |
| Permissions denied by default | `src/main/permissions.ts` | granted only to the server's own origin: `clipboard-sanitized-write` (the web UI's copy buttons) and `fullscreen` (the HTML fullscreen API). Refused: `notifications` (push cannot work, above), `media` (nothing records audio or video), geolocation, MIDI, HID/serial/USB devices, `clipboard-read`, and every permission any other origin asks for. |
| No remote module | — | `@electron/remote` is not a dependency; the built-in remote module was removed from Electron years ago. |
| Local pages on a custom scheme, never `file:` | `src/main/appAssets.ts` | a fixed file list, a strict CSP, `nosniff`. |
| Identity providers see a Chromium user agent | `start()` in `main.ts` | `Electron/…` and the app name are removed from the user agent; some providers refuse sign-in pages to user agents that name Electron. Whether a given provider (Google in particular) still refuses an embedded browser is **not verified**. |
| Untrusted certificates are refused | Chromium default | there is no "continue anyway"; a private certificate authority goes in the operating system's trust store. |
| Production fuses | `electron-builder.yml` | `RunAsNode` off, `EnableCookieEncryption` on (the session cookie is encrypted at rest with the OS keychain / DPAPI / libsecret), `EnableNodeOptionsEnvironmentVariable` off, `EnableNodeCliInspectArguments` off, `EnableEmbeddedAsarIntegrityValidation` on, `OnlyLoadAppFromAsar` on, `LoadBrowserProcessSpecificV8Snapshot` off, `GrantFileProtocolExtraPrivileges` off. The packaging workflow prints them back from each packaged binary. |

Chromium's `--remote-debugging-port` and `--remote-debugging-pipe` switches are not fuses, so the
app checks for them itself (`src/main/debugging.ts`), before anything else runs: with either switch
it **refuses to start** unless `EPICURUS_DESKTOP_USER_DATA_DIR` points at a directory other than the
default profile (symlinks and, on macOS and Windows, case resolved) — the rule Chrome applies to its
own default profile. A DevTools port would hand whoever opened it the signed-in session, decrypted
with the app's own keychain access. The launch smoke runs on a throwaway profile, so it can still
drive the packaged app this way, and it asserts the refusal. What this does not stop: a process
running as the user that copies the profile to another directory first; such a process already has
the user's files.

## Configuration

There is no configuration file to edit and no server-side setting. Everything the user chooses is
kept by the app (below). Two environment variables exist for tests and portable installs:

| Variable | Default | Effect |
| --- | --- | --- |
| `EPICURUS_DESKTOP_USER_DATA_DIR` | unset (the OS default below) | keep settings, cookies and cache in this directory instead; also the only way to start the app with a remote-debugging switch (above). |
| `EPICURUS_DESKTOP_OPEN_EXTERNAL_LOG` | unset | append each URL that would open in the system browser to this file **instead of** opening it — what the launch smoke uses to prove a new-window link left the app. |

## Data model

The app owns no server-side data. On the user's machine, in the user-data directory —
`~/Library/Application Support/Epicurus` (macOS), `%APPDATA%\Epicurus` (Windows),
`~/.config/Epicurus` (Linux):

- **`settings.json`** (mode `0600`), written atomically:

  ```json
  {
    "version": 1,
    "server": { "url": "https://epicurus.example.com/", "confirmedInsecure": false },
    "window": { "x": 120, "y": 80, "width": 1280, "height": 860, "maximized": false }
  }
  ```

  Read forgivingly: an unreadable file, or any field that does not validate (an address that does
  not normalise, a non-numeric size), falls back to its default instead of stopping the app; a
  size below 480×360 is raised to it.
- **`Partitions/epicurus/`** — the Chromium profile of the `persist:epicurus` session: cookies
  (the server's session cookie, encrypted at rest), local storage, the web UI's service worker
  and cache.

Removing the directory resets the app to first run and signs it out. The app is single-user by
nature; the server's tenant and sign-in decide whose data the window shows.

## Dependencies

- A reachable epicurus server (its [web shell](../services/web.md) behind its gateway or nginx).
- At build time: Node 22, Electron 45, electron-builder 26, TypeScript 6; tests use Vitest and
  Playwright (`@playwright/test`, with no browser download — the smoke drives the app's own
  Chromium).

## Packaging

`npm run package` (electron-builder, `electron-builder.yml`) builds for the platform it runs on;
`.github/workflows/desktop.yml` builds all three:

| Platform | Artifacts | Signing |
| --- | --- | --- |
| macOS | `Epicurus-<v>-mac-arm64.dmg`/`.zip`, `Epicurus-<v>-mac-x64.dmg`/`.zip` | **ad-hoc** (`identity: "-"`, hardened runtime off) — an unsigned arm64 app does not start at all. Not notarised: Gatekeeper warns on first open ([what the user does](../user/desktop-app.md#macos)). |
| Windows | `Epicurus-<v>-windows-x64-setup.exe` (NSIS, one-click, per-user, no admin) | unsigned — SmartScreen warns. |
| Linux | `Epicurus-<v>-linux-x86_64.AppImage`, `Epicurus-<v>-linux-amd64.deb` | — (the `.deb` installs electron-builder's AppArmor profile). |

There is no auto-update and nothing is published to a release yet.

## CI

- **`ci.yml` → `desktop`** (Ubuntu): ESLint, type-check, the Vitest unit tests, build, then the
  launch smoke against the development build under `xvfb`, with the real web UI built from
  `services/web` for its last test. Screenshots are uploaded as `desktop-smoke-linux-dev`.
- **`desktop.yml`** (on changes to `apps/desktop/**` or the workflow, pushes to `main`/`testing`,
  and by hand): packages on `macos-latest` (arm64), `windows-latest` and `ubuntu-latest`, prints
  the fuses, verifies the macOS ad-hoc signatures (`codesign --verify --deep --strict`, both
  architectures), then launches the **packaged** app — the arm64 build unpacked from its zip, the
  build the Windows installer installed (`/S`), the Linux AppImage — and runs the same smoke
  against it, with a page screenshot per step and a whole-screen screenshot of the window.
  Installers (`desktop-installers-<OS>`, kept 14 days) and screenshots
  (`desktop-screenshots-<OS>`) are uploaded. The x64 macOS build is built and signature-checked
  but not launched (the runner is arm64).

The **launch smoke** (`test/e2e/smoke.spec.ts`) starts the app with
`--remote-debugging-port=0` and drives it with Playwright over the DevTools protocol — not with
Playwright's Electron launcher, which attaches through Node's `--inspect` and so cannot reach a
packaged build whose `EnableNodeCliInspectArguments` fuse is off. Against a stand-in server
(`test/e2e/fixture-server.ts`, a Node HTTP server that answers `/platform/v1/info` and serves a
page) it proves: first run → connect (a bare `127.0.0.1:<port>/deep/link`, normalised) → the page
is shown and saved; the page has the marker and no shell API or Node; web push is refused; a
new-window link goes to the system browser and opens nothing in the app; a `file:` link is
refused; leaving for another host shows the bar and Back returns; a restart is still connected
with its cookie; a stopped server shows the offline page and Retry recovers; Change server refuses
a non-epicurus server, an `ftp:` address and a closed port, saving nothing. With
`EPICURUS_WEB_DIST` set (CI sets it), a test serves the real built web UI from the fixture
and screenshots it and its push card. The last test starts the app with `--remote-debugging-port`
on its default profile and asserts it exits without opening the port.

## Run & extend

```bash
cd apps/desktop
npm ci
npm start                 # build, then open the app (needs a display)
npm test                  # unit tests (address, navigation, permissions, settings, server check, …)
npm run lint && npm run typecheck
npm run build && npm run smoke   # the launch smoke; on a headless Linux box use xvfb-run
npm run package           # installers for this platform, in release/
```

Layout: `src/main/` (the main process — `main.ts` wires the window; every decision lives in a pure,
unit-tested module beside it), `src/preload/preload.ts`, `src/renderer/` (the three local pages),
`test/unit/` (Vitest), `test/e2e/` (the smoke), `resources/icon.png` (from the web UI's icon).

Changing it safely:

- Keep decisions pure and tested: a new navigation, permission or address rule goes into its
  module and its unit test, not inline in `main.ts`.
- A new permission is granted only to the server's origin, named in `permissions.ts` with the UI
  feature that needs it, and added to the table above.
- A new IPC call goes in `src/main/channels.ts` **and** the preload's copy (a unit test keeps them
  equal), behind the app-page sender check.
- A new capability for the web UI goes in `window.epicurusDesktop.capabilities`; the web UI must
  feature-detect it and behave as a browser without it.
- Anything that changes what the window does gets a step in the launch smoke, so it runs on all
  three platforms in `desktop.yml`.

Not built yet (follow-ups): a local client core (projects without a server), auto-update, code
signing and notarisation, deep links, native notifications, publishing installers on release tags.
