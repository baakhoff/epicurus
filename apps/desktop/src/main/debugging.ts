/**
 * Refusing Chromium's remote-debugging switches on the user's real profile.
 *
 * The production fuses close Node's `--inspect`, but Chromium's `--remote-debugging-port` and
 * `--remote-debugging-pipe` are not fuses: anything that can start the app could start it with
 * one and read the signed-in session (cookies decrypted by the app's own keychain access) over
 * the DevTools protocol. Chrome refuses these switches on its default profile for the same
 * reason (Chrome 136). The app does the same: with a debugging switch it starts only when
 * `EPICURUS_DESKTOP_USER_DATA_DIR` points at a directory other than the default one — which is
 * how the launch smoke runs, on a throwaway profile.
 */
import * as fs from "node:fs";
import * as path from "node:path";

export const REMOTE_DEBUGGING_SWITCHES = ["remote-debugging-port", "remote-debugging-pipe"];

function canonical(dir: string, platform: NodeJS.Platform): string {
  let resolved = path.resolve(dir);
  try {
    resolved = fs.realpathSync.native(resolved);
  } catch {
    /* a directory that does not exist yet holds no profile */
  }
  resolved = resolved.replace(/[\\/]+$/, "");
  return platform === "win32" || platform === "darwin" ? resolved.toLowerCase() : resolved;
}

/**
 * Whether the app must refuse to start: a remote-debugging switch is present and the profile it
 * would expose is the user's default one (no override, or an override that resolves to it).
 */
export function mustRefuseRemoteDebugging(
  hasSwitch: (name: string) => boolean,
  userDataOverride: string | undefined,
  defaultUserData: string,
  platform: NodeJS.Platform = process.platform,
): boolean {
  if (!REMOTE_DEBUGGING_SWITCHES.some((name) => hasSwitch(name))) return false;
  if (!userDataOverride) return true;
  return canonical(userDataOverride, platform) === canonical(defaultUserData, platform);
}
