import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "release", "node_modules", "test-results", "playwright-report"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,mts}"],
    languageOptions: { ecmaVersion: 2023, globals: globals.node },
    rules: {
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },
  {
    // The local pages' scripts run in a sandboxed renderer: DOM, no Node.
    files: ["src/renderer/**/*.ts"],
    languageOptions: { globals: globals.browser },
  },
  {
    // The main process and preload must never reach for a remote-content escape hatch.
    files: ["src/main/**/*.ts", "src/preload/**/*.ts"],
    rules: {
      "no-restricted-properties": [
        "error",
        { object: "shell", property: "openPath", message: "Never open local paths from web content." },
      ],
      "no-restricted-syntax": [
        "error",
        {
          selector: "Property[key.name='nodeIntegration'][value.value=true]",
          message: "nodeIntegration stays off (docs/apps/desktop.md).",
        },
        {
          selector: "Property[key.name='contextIsolation'][value.value=false]",
          message: "contextIsolation stays on (docs/apps/desktop.md).",
        },
        {
          selector: "Property[key.name='sandbox'][value.value=false]",
          message: "The renderer stays sandboxed (docs/apps/desktop.md).",
        },
        {
          selector: "Property[key.name='webSecurity'][value.value=false]",
          message: "webSecurity stays on (docs/apps/desktop.md).",
        },
      ],
    },
  },
  {
    files: ["scripts/**/*.mjs", "eslint.config.mjs"],
    languageOptions: { globals: globals.node },
  },
);
