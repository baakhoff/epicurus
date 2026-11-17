/**
 * The preload — the only code that runs beside page scripts, sandboxed, with no Node.
 *
 * - Every page gets `window.epicurusDesktop`, a frozen, read-only description of the shell the
 *   web UI can feature-detect on. Today it only says that web push is not available here
 *   (`services/web` hides its push controls when it sees this), and which platform this is.
 * - The app's own local pages (`epicurus-app://local/…`) also get `window.epicurusShell`: the
 *   handful of calls the connect, offline and host-bar pages make. A web page never gets it, and
 *   the main process refuses these calls from anything but a local page regardless.
 *
 * Self-contained on purpose: a sandboxed preload can `require("electron")` and nothing local.
 */
import { contextBridge, ipcRenderer } from "electron";

const CHANNELS = {
  checkServer: "epicurus-shell:check-server",
  connect: "epicurus-shell:connect",
  cancelConnect: "epicurus-shell:cancel-connect",
  retry: "epicurus-shell:retry",
  changeServer: "epicurus-shell:change-server",
  backToServer: "epicurus-shell:back-to-server",
} as const;

contextBridge.exposeInMainWorld(
  "epicurusDesktop",
  Object.freeze({
    shell: "electron",
    platform: process.platform,
    capabilities: Object.freeze({ webPush: false }),
  }),
);

// The preload runs before any page script, in an isolated world that shares the DOM.
declare const location: { protocol: string };

if (location.protocol === "epicurus-app:") {
  contextBridge.exposeInMainWorld("epicurusShell", {
    checkServer: (address: string): Promise<unknown> =>
      ipcRenderer.invoke(CHANNELS.checkServer, String(address)),
    connect: (url: string, confirmedInsecure: boolean): Promise<unknown> =>
      ipcRenderer.invoke(CHANNELS.connect, String(url), confirmedInsecure === true),
    cancelConnect: (): Promise<unknown> => ipcRenderer.invoke(CHANNELS.cancelConnect),
    retry: (): Promise<unknown> => ipcRenderer.invoke(CHANNELS.retry),
    changeServer: (): Promise<unknown> => ipcRenderer.invoke(CHANNELS.changeServer),
    backToServer: (): Promise<unknown> => ipcRenderer.invoke(CHANNELS.backToServer),
  });
}
