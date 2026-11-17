/** Serving the app's own pages on `epicurus-app://local/` — a fixed list of files, nothing else. */
import * as path from "node:path";

import { APP_HOST, APP_SCHEME } from "./navigation";

/** Every file the local pages are made of. Anything else under the scheme is a 404. */
export const APP_ASSETS: Readonly<Record<string, string>> = {
  "connect.html": "text/html; charset=utf-8",
  "connect.js": "text/javascript; charset=utf-8",
  "offline.html": "text/html; charset=utf-8",
  "offline.js": "text/javascript; charset=utf-8",
  "bar.html": "text/html; charset=utf-8",
  "bar.js": "text/javascript; charset=utf-8",
  "style.css": "text/css; charset=utf-8",
};

/** Local pages run no inline script and load nothing from anywhere but the app itself. */
export const APP_CSP =
  "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; " +
  "base-uri 'none'; form-action 'none'; frame-ancestors 'none'";

/** The file and content type for a request to the app scheme, or `null` (→ 404). */
export function resolveAppAsset(
  requestUrl: string,
  root: string,
): { file: string; contentType: string } | null {
  let url: URL;
  try {
    url = new URL(requestUrl);
  } catch {
    return null;
  }
  if (url.protocol !== `${APP_SCHEME}:` || url.host !== APP_HOST) return null;
  const name = decodeURIComponent(url.pathname).replace(/^\/+/, "");
  const contentType = APP_ASSETS[name];
  if (contentType === undefined || !Object.hasOwn(APP_ASSETS, name)) return null;
  return { file: path.join(root, name), contentType };
}
