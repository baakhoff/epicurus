import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Vendored variable fonts — every byte ships with the app, nothing remote.
import "@fontsource-variable/inter";
import "@fontsource-variable/literata";
import "@fontsource-variable/jetbrains-mono";
import "./index.css";

import App from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
