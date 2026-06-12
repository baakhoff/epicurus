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
      // The service worker must NEVER interpose on /platform — SSE streams
      // (chat, model pulls) pass through to the network untouched.
      workbox: {
        globPatterns: ["**/*.{js,css,html,svg,png,woff2,webmanifest}"],
        navigateFallback: "index.html",
        navigateFallbackDenylist: [/^\/platform\//],
        runtimeCaching: [],
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
  // Vitest (https://vitest.dev/config/)
  test: {
    environment: "jsdom",
    globals: true, // enables testing-library auto-cleanup between tests
    setupFiles: ["src/test/setup.ts"],
    css: false,
  },
});
