import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { VitePWA } from "vite-plugin-pwa";
import path from "node:path";

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      registerType: "prompt",
      // injectManifest (#493): generateSW's declarative config can't express a custom fetch
      // handler, and the share target below needs one (a service worker is the only way to
      // read a POST body before the browser discards it navigating away). src/sw.ts owns the
      // SPA-fallback + /platform-exclusion behavior the old `workbox` block gave for free —
      // see its own top comment — and the update-prompt skipWaiting wiring.
      strategies: "injectManifest",
      srcDir: "src",
      filename: "sw.ts",
      injectManifest: {
        globPatterns: ["**/*.{js,css,html,svg,png,woff2,webmanifest}"],
      },
      manifest: {
        name: "epicurus",
        short_name: "epicurus",
        description: "Your self-hosted, local-first assistant.",
        display: "standalone",
        start_url: "/",
        background_color: "#121411",
        theme_color: "#121411",
        icons: [
          { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
          {
            src: "/icons/icon-maskable-512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "maskable",
          },
        ],
        // Share a link, text, or image/file from any app straight into a chat turn (#493).
        // The service worker (src/sw.ts) intercepts this POST — there is no server route
        // behind it — stashes the payload, and redirects to /?share=1 for the chat screen
        // to pick up. `*/*` rather than an image-only accept list: "image/file" per the issue.
        share_target: {
          action: "/share-target",
          method: "POST",
          enctype: "multipart/form-data",
          params: {
            title: "title",
            text: "text",
            url: "url",
            files: [{ name: "file", accept: ["*/*"] }],
          },
        },
        // Long-press the icon → straight to the three most-reached-for destinations (#493).
        // "Calendar"/"Tasks" are module pages — if that module is off, ModulePageScreen's
        // existing "no such module page" empty state is the degrade, not a crash (no new
        // code needed for that half of the acceptance criteria).
        shortcuts: [
          { name: "New chat", url: "/" },
          { name: "Calendar", url: "/m/calendar/calendar" },
          { name: "Tasks", url: "/m/tasks/board" },
        ],
      },
    }),
  ],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  server: {
    // Local dev against a running stack: the core is published on :8082.
    proxy: {
      "/platform": { target: "http://localhost:8082", changeOrigin: true },
    },
  },
  // `vite preview` doesn't inherit `server.proxy` — it needs its own. Without this, checking
  // a production build locally (`npm run build && npm run preview`, the only way the real
  // generated service worker — injectManifest, #493 — ever runs) has no path to the core.
  preview: {
    proxy: {
      "/platform": { target: "http://localhost:8082", changeOrigin: true },
    },
  },
  // Vitest (https://vitest.dev/config/)
  test: {
    environment: "jsdom",
    globals: true, // enables testing-library auto-cleanup between tests
    setupFiles: ["src/test/setup.ts"],
    css: false,
  },
});
