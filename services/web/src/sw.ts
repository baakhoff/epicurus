/// <reference lib="webworker" />
/**
 * The custom service worker (#493, injectManifest strategy — vite-plugin-pwa can no longer
 * generate one wholesale, since it must intercept a POST for the share target below, which
 * `generateSW`'s declarative config has no way to express).
 *
 * Reproduces the two behaviors the old `generateSW` config gave for free:
 * - **SPA navigation fallback** (`navigateFallback: "index.html"`): a top-level navigation to
 *   an unknown path serves the shell instead of a raw 404, so client-side routing works on a
 *   reload/deep-link. `/platform/*` is never a `navigate`-mode request (the app's own
 *   fetch/SSE calls to it use `cors`/`same-origin` mode, never a top-level navigation), so it
 *   needs no explicit denylist here the way `generateSW`'s config had one.
 * - **The `registerType: "prompt"` update flow**: the shell's `UpdateToast` (`App.tsx`) posts
 *   `{ type: "SKIP_WAITING" }` to the waiting worker when the operator clicks Refresh — this
 *   file must listen for it and call `skipWaiting()` only then, never unconditionally, or
 *   every update would activate itself immediately without asking (defeating "prompt" mode).
 *
 * Also owns web push (#670, ADR-0102): the `push`/`notificationclick` handlers below. Like
 * the share target, this is the only place a push payload can be turned into a system
 * notification and a deep link — there is no server-side or React-tree equivalent.
 *
 * This file is excluded from the app's `tsconfig.json` (its DOM lib and a service worker's
 * WebWorker lib can't coexist in one project) — Vite still bundles it via `injectManifest`
 * regardless, since that's wired independently in `vite.config.ts`. `no-restricted-globals`
 * (bare `fetch`, #529) is scoped to the app's browser-tab code — this file has no `epFetch`,
 * no `useConnection` store, no React tree to feed; it is its own global scope entirely.
 */
import { clientsClaim } from "workbox-core";
import { cleanupOutdatedCaches, precacheAndRoute } from "workbox-precaching";

import { SHARE_CACHE, SHARE_FILE_KEY, SHARE_FILE_NAME_HEADER, SHARE_META_KEY } from "@/lib/shareTarget";

declare const self: ServiceWorkerGlobalScope;

cleanupOutdatedCaches();
precacheAndRoute(self.__WB_MANIFEST);
clientsClaim();

self.addEventListener("message", (event) => {
  if (event.data?.type === "SKIP_WAITING") self.skipWaiting();
});

/**
 * Share target (#493): the OS share sheet POSTs here (`manifest.share_target.action`) with
 * `title`/`text`/`url` + an optional `file`. A service worker is the only way to read a POST
 * body before the browser discards it navigating to the destination — there is no server-side
 * handler behind this route, so it must be handled entirely here. Stashes the payload in the
 * Cache API (survives the redirect + the chat screen's own mount) and redirects to `/?share=1`;
 * the chat screen picks it up, uploads any file through the existing attachment path, and
 * prefills the composer — it never sends on the operator's behalf (the #480 starter-chip rule).
 */
async function handleShareTarget(event: FetchEvent): Promise<Response> {
  const formData = await event.request.formData();
  const asText = (key: string): string => {
    const value = formData.get(key);
    return typeof value === "string" ? value : "";
  };
  const file = formData.get("file");
  const hasFile = file instanceof File && file.size > 0;

  const cache = await caches.open(SHARE_CACHE);
  await cache.put(
    SHARE_META_KEY,
    new Response(
      JSON.stringify({ title: asText("title"), text: asText("text"), url: asText("url"), hasFile }),
      { headers: { "Content-Type": "application/json" } },
    ),
  );
  if (hasFile) {
    await cache.put(
      SHARE_FILE_KEY,
      new Response(file, {
        headers: {
          "Content-Type": file.type || "application/octet-stream",
          [SHARE_FILE_NAME_HEADER]: encodeURIComponent(file.name || "shared-file"),
        },
      }),
    );
  } else {
    await cache.delete(SHARE_FILE_KEY);
  }

  // 303 (not a bare redirect): the browser follows with a GET, so the client never re-submits
  // the share POST on a reload of the destination (the standard Post-Redirect-Get pattern).
  return Response.redirect("/?share=1", 303);
}

self.addEventListener("fetch", (event: FetchEvent) => {
  if (event.request.method === "POST" && new URL(event.request.url).pathname === "/share-target") {
    event.respondWith(handleShareTarget(event));
    return;
  }

  // A share-target POST is itself reported as `navigate` mode by some browsers, so this must
  // run only after the check above, not before it.
  if (event.request.mode === "navigate") {
    event.respondWith(caches.match("/index.html").then((cached) => cached ?? fetch(event.request)));
  }
});

/**
 * Push (#670, ADR-0102): the core's send path posts one JSON payload per push —
 * `{title, body, category, deep_link, entity_ref}`, encrypted per RFC 8291 — and this is the
 * only place in the app that can turn it into a system notification; there is no React tree
 * running when the PWA isn't open. `event.data` can be absent (a payload-less push is legal
 * per spec, just unused today) and a malformed payload shouldn't drop the notification
 * silently, hence the fallbacks throughout.
 */
interface PushPayload {
  title?: string;
  body?: string;
  category?: string;
  deep_link?: string;
}

self.addEventListener("push", (event: PushEvent) => {
  let payload: PushPayload = {};
  try {
    if (event.data) payload = event.data.json() as PushPayload;
  } catch {
    // Malformed payload — still show a generic notification rather than silently drop it.
  }
  const title = payload.title || "epicurus";
  event.waitUntil(
    self.registration.showNotification(title, {
      body: payload.body,
      // A second push in the same category replaces the first in the OS tray rather than
      // stacking — e.g. three "New mail" pushes collapse to the latest, not a pile of three.
      tag: payload.category,
      data: { deep_link: payload.deep_link || "/" },
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
    }),
  );
});

/**
 * Notification click (#670): focus an existing PWA window and navigate it to the deep link,
 * or open a new one if none is open — reusing a window rather than always opening a fresh
 * one, so a tap never leaves the operator with two copies of the app running.
 */
self.addEventListener("notificationclick", (event: NotificationEvent) => {
  event.notification.close();
  const data = event.notification.data as { deep_link?: string } | undefined;
  const deepLink = data?.deep_link || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ("focus" in client) {
          void client.focus();
          if ("navigate" in client) return (client as WindowClient).navigate(deepLink);
          return undefined;
        }
      }
      return self.clients.openWindow(deepLink);
    }),
  );
});
