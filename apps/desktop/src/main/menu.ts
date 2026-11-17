/**
 * The application menu — built from a template so the platform differences are testable.
 *
 * The window is a `BaseWindow` holding the page and the host bar as two views, so the
 * window-scoped menu *roles* (reload, zoom, minimise…) — which act on a `BrowserWindow`'s own
 * web contents — are replaced by explicit handlers that act on the page. The Edit menu keeps
 * the native roles on macOS, where copy/paste are dispatched through the responder chain to
 * whichever view has focus and *only* work while the menu has the items; on Windows and Linux
 * the items call the focused web contents directly (the keys work there with or without a menu).
 */
import type { MenuItemConstructorOptions } from "electron";

export type EditCommand = "undo" | "redo" | "cut" | "copy" | "paste" | "selectAll";

export type MenuHandlers = {
  changeServer(): void;
  reload(): void;
  forceReload(): void;
  toggleDevTools(): void;
  /** `0` resets, `+1` / `-1` step the zoom level. */
  zoom(step: -1 | 0 | 1): void;
  toggleFullScreen(): void;
  minimize(): void;
  toggleMaximize(): void;
  closeWindow(): void;
  quit(): void;
  edit(command: EditCommand): void;
  openExternal(url: string): void;
};

export const DOCS_URL =
  "https://github.com/baakhoff/epicurus/blob/main/docs/user/desktop-app.md";
export const PROJECT_URL = "https://github.com/baakhoff/epicurus";

function editMenu(platform: NodeJS.Platform, h: MenuHandlers): MenuItemConstructorOptions {
  if (platform === "darwin") {
    return {
      label: "Edit",
      submenu: [
        { role: "undo" },
        { role: "redo" },
        { type: "separator" },
        { role: "cut" },
        { role: "copy" },
        { role: "paste" },
        { role: "pasteAndMatchStyle" },
        { role: "delete" },
        { role: "selectAll" },
      ],
    };
  }
  const item = (
    label: string,
    command: EditCommand,
    accelerator: string,
  ): MenuItemConstructorOptions => ({
    label,
    accelerator,
    // The renderer handles these keys itself on Windows and Linux; registering them again
    // would run every copy twice.
    registerAccelerator: false,
    click: () => h.edit(command),
  });
  return {
    label: "&Edit",
    submenu: [
      item("&Undo", "undo", "Ctrl+Z"),
      item("&Redo", "redo", "Ctrl+Y"),
      { type: "separator" },
      item("Cu&t", "cut", "Ctrl+X"),
      item("&Copy", "copy", "Ctrl+C"),
      item("&Paste", "paste", "Ctrl+V"),
      { type: "separator" },
      item("Select &All", "selectAll", "Ctrl+A"),
    ],
  };
}

export function buildMenuTemplate(
  platform: NodeJS.Platform,
  appName: string,
  h: MenuHandlers,
): MenuItemConstructorOptions[] {
  const mac = platform === "darwin";
  const template: MenuItemConstructorOptions[] = [];

  if (mac) {
    template.push({
      label: appName,
      submenu: [
        { role: "about" },
        { type: "separator" },
        { label: "Change Server…", click: () => h.changeServer() },
        { type: "separator" },
        { role: "services" },
        { type: "separator" },
        { role: "hide" },
        { role: "hideOthers" },
        { role: "unhide" },
        { type: "separator" },
        { label: `Quit ${appName}`, accelerator: "Cmd+Q", click: () => h.quit() },
      ],
    });
  } else {
    template.push({
      label: "&File",
      submenu: [
        { label: "&Change Server…", click: () => h.changeServer() },
        { type: "separator" },
        { label: "E&xit", accelerator: "Alt+F4", click: () => h.quit() },
      ],
    });
  }

  template.push(editMenu(platform, h));

  template.push({
    label: mac ? "View" : "&View",
    submenu: [
      { label: "Reload", accelerator: "CmdOrCtrl+R", click: () => h.reload() },
      { label: "Force Reload", accelerator: "Shift+CmdOrCtrl+R", click: () => h.forceReload() },
      {
        label: "Toggle Developer Tools",
        accelerator: mac ? "Alt+Cmd+I" : "Ctrl+Shift+I",
        click: () => h.toggleDevTools(),
      },
      { type: "separator" },
      { label: "Actual Size", accelerator: "CmdOrCtrl+0", click: () => h.zoom(0) },
      { label: "Zoom In", accelerator: "CmdOrCtrl+=", click: () => h.zoom(1) },
      // A second, hidden binding so Ctrl/Cmd and the "+" key (Shift+=) both zoom in.
      { label: "Zoom In", accelerator: "CmdOrCtrl+Plus", visible: false, click: () => h.zoom(1) },
      { label: "Zoom Out", accelerator: "CmdOrCtrl+-", click: () => h.zoom(-1) },
      { type: "separator" },
      {
        label: "Toggle Full Screen",
        accelerator: mac ? "Ctrl+Cmd+F" : "F11",
        click: () => h.toggleFullScreen(),
      },
    ],
  });

  template.push({
    label: mac ? "Window" : "&Window",
    submenu: [
      { label: "Minimize", accelerator: "CmdOrCtrl+M", click: () => h.minimize() },
      { label: mac ? "Zoom" : "Maximize", click: () => h.toggleMaximize() },
      { type: "separator" },
      ...(mac ? [{ role: "front" as const }, { type: "separator" as const }] : []),
      { label: "Close Window", accelerator: "CmdOrCtrl+W", click: () => h.closeWindow() },
    ],
  });

  template.push({
    label: mac ? "Help" : "&Help",
    submenu: [
      { label: "Desktop App Documentation", click: () => h.openExternal(DOCS_URL) },
      { label: "epicurus on GitHub", click: () => h.openExternal(PROJECT_URL) },
    ],
  });

  return template;
}
