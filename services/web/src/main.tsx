import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Vendored variable fonts — every byte ships with the app, nothing remote.
import "@fontsource-variable/inter";
import "@fontsource-variable/literata";
import "@fontsource-variable/jetbrains-mono";
import "./index.css";

import App from "./App";
import { usePrefs } from "./stores/prefs";

// Apply the persisted theme before first paint so the shell never flashes the
// default theme (App keeps it in sync on change).
document.documentElement.setAttribute("data-theme", usePrefs.getState().theme);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
