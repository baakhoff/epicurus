# The desktop app

**Epicurus** for macOS, Windows and Linux is a window around your epicurus server's web app —
the same chat, models, modules and settings you reach in a browser, in its own window with
its own menu, remembering your server and your sign-in. It does not run epicurus itself: you
still need a server (see [Installation](installation.md)), and the app connects to it.

> **Status.** Version 0.1.0, the first build. Installers are produced by the project's CI for
> every change to the app; they are not attached to a release yet and are **not signed** with a
> certificate, so each operating system warns the first time you open it — the steps below
> explain what to do. There is no automatic update: install a newer build over the old one.

## Get it

On the repository's **Actions** tab, open the latest successful **Desktop** run and download
the installer for your system from its artifacts (`desktop-installers-macOS`,
`desktop-installers-Windows`, `desktop-installers-Linux`; you need to be signed in to GitHub).
Each is a zip that holds the installers below.

### macOS

Two builds: `Epicurus-0.1.0-mac-arm64.dmg` for Apple silicon (M1 and later) and
`Epicurus-0.1.0-mac-x64.dmg` for Intel Macs (a `.zip` of each is there too).

1. Open the `.dmg` and drag **Epicurus** to **Applications**.
2. The first time, macOS refuses to open it — *"Apple could not verify 'Epicurus' is free of
   malware"* — because the app is not notarised by Apple (it is ad-hoc signed, which is what lets
   it run on Apple silicon at all). Close the message, then open **System Settings → Privacy &
   Security**, scroll to *"Epicurus" was blocked…* and click **Open Anyway**, then confirm with
   your password. From then on it opens normally.
3. If macOS instead says the app *"is damaged and can't be opened"*, the download was quarantined
   in a way the button does not clear. In Terminal, run
   `xattr -dr com.apple.quarantine /Applications/Epicurus.app` and open it again.

### Windows

Run `Epicurus-0.1.0-windows-x64-setup.exe`. It installs for your user only (no administrator
rights) and starts the app. Because the installer is not signed, **Microsoft Defender
SmartScreen** may say *"Windows protected your PC"*: click **More info**, then **Run anyway**.
Remove it later from **Settings → Apps**.

### Linux

- **Debian / Ubuntu:** `sudo apt install ./Epicurus-0.1.0-linux-amd64.deb`, then start
  **Epicurus** from your applications menu or run `epicurus`. The package installs an AppArmor
  profile, which Ubuntu 24.04 and later need for the app's sandbox.
- **Anything else:** make `Epicurus-0.1.0-linux-x86_64.AppImage` executable
  (`chmod +x`) and run it. AppImages need FUSE (`libfuse2` on most distributions). On Ubuntu
  24.04 and later the AppImage may fail to start with a sandbox error, because the system
  restricts the unprivileged user namespaces Chromium's sandbox uses and an AppImage brings no
  AppArmor profile — use the `.deb` there.

## Connect to your server

The first time it opens, the app asks for your server's address — what you type in a browser to
reach epicurus, for example `https://epicurus.example.com` or `http://localhost:8088`. You can
paste any link into the app; only the server part is kept. Without `https://` or `http://`,
`https://` is assumed (and `http://` for `localhost`).

The app checks the address before saving it and tells you if the server cannot be reached, if it
is not an epicurus server, or if its certificate is not trusted by your computer. A server with a
certificate from your own certificate authority works once that authority is trusted by your
operating system; the app has no "continue anyway" button.

If the address starts with `http://` and is not your own computer, the app warns that the
connection is not encrypted and asks you to confirm — anyone on the network in between could read
what you send, including your sign-in. Prefer `https://`; see
[Remote access & hardening](../infrastructure/remote-access.md).

To switch servers later, use **Change Server…** (in the **Epicurus** menu on macOS, the **File**
menu elsewhere).

## Using it

- **Signing in** works as in a browser: if your server has [sign-in](../infrastructure/sign-in.md)
  turned on, the window goes to your identity provider and comes back. While the window shows a
  page that is not your server, a bar at the top names the site you are on, with a **Back to
  Epicurus** button. You stay signed in when you quit and reopen the app.
- **Connecting an account** (Modules or Settings) also happens in the window, the same way.
- **Links** that would open a new tab open in your normal browser.
- **If the server is down**, the app shows a page with **Retry** and **Change server** instead of
  a blank window.
- **Zoom, reload and full screen** are in the **View** menu; the window remembers its size and
  position.
- **Push notifications are not available in the desktop app.** Settings → Push notifications says
  so; subscribe from a browser or your phone to be notified there. Everything still appears in the
  notification center.

## Reset it

Quit the app and delete its folder — `~/Library/Application Support/Epicurus` (macOS),
`%APPDATA%\Epicurus` (Windows), `~/.config/Epicurus` (Linux). It opens at "Connect to a server"
again, signed out. What is in that folder: [the desktop app's data](../apps/desktop.md#data-model).
