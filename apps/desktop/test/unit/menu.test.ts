import type { MenuItemConstructorOptions } from "electron";
import { describe, expect, it, vi, type Mock } from "vitest";

import { DOCS_URL, buildMenuTemplate, type MenuHandlers } from "../../src/main/menu";

function handlers(): MenuHandlers & { [K in keyof MenuHandlers]: Mock } {
  return {
    changeServer: vi.fn(),
    reload: vi.fn(),
    forceReload: vi.fn(),
    toggleDevTools: vi.fn(),
    zoom: vi.fn(),
    toggleFullScreen: vi.fn(),
    minimize: vi.fn(),
    toggleMaximize: vi.fn(),
    closeWindow: vi.fn(),
    quit: vi.fn(),
    edit: vi.fn(),
    openExternal: vi.fn(),
  };
}

function items(menu: MenuItemConstructorOptions): MenuItemConstructorOptions[] {
  return (menu.submenu as MenuItemConstructorOptions[]) ?? [];
}

function find(template: MenuItemConstructorOptions[], label: string): MenuItemConstructorOptions {
  const menu = template.find((m) => (m.label ?? "").replace("&", "") === label);
  if (!menu) throw new Error(`no ${label} menu in ${template.map((m) => m.label).join(", ")}`);
  return menu;
}

function click(item: MenuItemConstructorOptions | undefined): void {
  (item?.click as unknown as () => void)();
}

describe("buildMenuTemplate", () => {
  it("on macOS: an app menu, and an Edit menu made of native roles so copy/paste work", () => {
    const template = buildMenuTemplate("darwin", "Epicurus", handlers());
    expect(template.map((m) => m.label)).toEqual(["Epicurus", "Edit", "View", "Window", "Help"]);
    const roles = items(find(template, "Edit")).map((i) => i.role).filter(Boolean);
    expect(roles).toEqual(
      expect.arrayContaining(["undo", "redo", "cut", "copy", "paste", "selectAll"]),
    );
  });

  it("on Windows and Linux: File/Edit/View/Window/Help, edit items calling the focused page", () => {
    for (const platform of ["win32", "linux"] as const) {
      const h = handlers();
      const template = buildMenuTemplate(platform, "Epicurus", h);
      expect(template.map((m) => (m.label ?? "").replace("&", ""))).toEqual([
        "File",
        "Edit",
        "View",
        "Window",
        "Help",
      ]);
      const edit = items(find(template, "Edit"));
      const copy = edit.find((i) => i.label === "&Copy");
      expect(copy).toMatchObject({ accelerator: "Ctrl+C", registerAccelerator: false });
      click(copy);
      expect(h.edit).toHaveBeenCalledWith("copy");
      click(edit.find((i) => i.label === "&Paste"));
      expect(h.edit).toHaveBeenCalledWith("paste");
    }
  });

  it("offers Change Server… on every platform", () => {
    for (const platform of ["darwin", "win32", "linux"] as const) {
      const h = handlers();
      const all = buildMenuTemplate(platform, "Epicurus", h).flatMap(items);
      click(all.find((i) => (i.label ?? "").replace("&", "") === "Change Server…"));
      expect(h.changeServer).toHaveBeenCalledOnce();
    }
  });

  it("View reloads and zooms the page", () => {
    const h = handlers();
    const view = items(find(buildMenuTemplate("linux", "Epicurus", h), "View"));
    click(view.find((i) => i.label === "Reload"));
    click(view.find((i) => i.label === "Actual Size"));
    click(view.find((i) => i.label === "Zoom In"));
    click(view.find((i) => i.label === "Zoom Out"));
    expect(h.reload).toHaveBeenCalledOnce();
    expect(h.zoom.mock.calls).toEqual([[0], [1], [-1]]);
    expect(view.find((i) => i.label === "Reload")?.accelerator).toBe("CmdOrCtrl+R");
  });

  it("Window minimises, maximises and closes; Help opens the docs outside the app", () => {
    const h = handlers();
    const template = buildMenuTemplate("darwin", "Epicurus", h);
    const windowItems = items(find(template, "Window"));
    click(windowItems.find((i) => i.label === "Minimize"));
    click(windowItems.find((i) => i.label === "Zoom"));
    click(windowItems.find((i) => i.label === "Close Window"));
    expect(h.minimize).toHaveBeenCalledOnce();
    expect(h.toggleMaximize).toHaveBeenCalledOnce();
    expect(h.closeWindow).toHaveBeenCalledOnce();
    click(items(find(template, "Help"))[0]);
    expect(h.openExternal).toHaveBeenCalledWith(DOCS_URL);
  });
});
