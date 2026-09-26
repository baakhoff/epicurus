/**
 * What the service worker's `fetch` handler (`src/sw.ts`) does with a request — the whole
 * decision, as a pure function of the request's method, mode and URL, so it can be unit-tested
 * without a service worker (#969).
 *
 * - `"share-target"` — the OS share sheet's POST (#493). Handled entirely in the worker; there
 *   is no server route behind it.
 * - `"app-shell"` — a top-level navigation to an app path: answer with the cached `index.html`,
 *   so a reload or a deep link routes client-side even offline.
 * - `"pass-through"` — the worker does not answer; the browser takes the request to the network
 *   as if no worker existed (precached assets are still answered by Workbox's own route, which
 *   runs ahead of this one).
 *
 * **Navigations to `/platform/` pass through.** They are real and they matter: the
 * connected-account OAuth callback (`/platform/v1/oauth/callback`) and the sign-in routes
 * (`/platform/v1/auth/login`, `/platform/v1/auth/callback`) are top-level redirects, and an
 * operator can open any platform URL in a tab. Until #969 every one of them was answered with
 * the cached app shell — so once a device had installed the worker, Google's redirect back
 * never reached the core, and the account never connected.
 */
export type SwRoute = "share-target" | "app-shell" | "pass-through";

/** The three facts the decision reads — a `Request` satisfies this as-is. */
export interface SwRequestFacts {
  method: string;
  mode: string;
  url: string;
}

export function swRoute({ method, mode, url }: SwRequestFacts): SwRoute {
  const { pathname } = new URL(url);
  // First, and regardless of mode: some browsers report the share POST as a navigation.
  if (method === "POST" && pathname === "/share-target") return "share-target";
  if (mode !== "navigate") return "pass-through";
  if (pathname.startsWith("/platform/")) return "pass-through";
  return "app-shell";
}
