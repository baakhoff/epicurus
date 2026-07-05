import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "dev-dist", "coverage"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      // react-hooks 7's `recommended` preset, which folds in the React Compiler
      // rules (static-components, set-state-in-effect, immutability, …) on top of
      // rules-of-hooks + exhaustive-deps.
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      // Keep every form control on the one themed field style (#394): no bare <input>/
      // <select> that would fall back to the browser-default (white-bordered) control.
      // Non-text inputs (range/file/checkbox/radio) opt out with an eslint-disable + reason.
      // Native dialogs are banned the same way (#488): errors surface as themed toasts
      // (toast.error/info from @/stores/toasts) and confirmations route through the
      // shared <Confirm> primitive — window.alert/window.confirm break the theme.
      "no-restricted-syntax": [
        "error",
        {
          selector: "JSXOpeningElement[name.name='select']",
          message:
            "Use the shared <Select> from @/components/ui instead of a raw <select> (#394).",
        },
        {
          selector: "JSXOpeningElement[name.name='input']",
          message:
            "Use <TextInput>/<NumberInput> from @/components/ui instead of a raw <input> (#394). For a non-text input (range/file/checkbox/radio) add an eslint-disable with a reason.",
        },
        {
          selector: "MemberExpression[object.name='window'][property.name='alert']",
          message:
            "Use toast.error()/toast.info() from @/stores/toasts instead of window.alert (#488).",
        },
        {
          selector: "MemberExpression[object.name='window'][property.name='confirm']",
          message:
            "Use the <Confirm> primitive from @/components/ui instead of window.confirm (#488).",
        },
      ],
      "no-restricted-globals": [
        "error",
        {
          name: "alert",
          message: "Use toast.error()/toast.info() from @/stores/toasts instead (#488).",
        },
        {
          name: "confirm",
          message: "Use the <Confirm> primitive from @/components/ui instead (#488).",
        },
        {
          name: "fetch",
          message: "Use epFetch() from @/lib/http instead of bare fetch() — it feeds the outage/connection detector (#494, #529).",
        },
      ],
    },
  },
  {
    // The primitives in ui.tsx are the one sanctioned home for the raw <input>/<select>.
    files: ["src/components/ui.tsx"],
    rules: { "no-restricted-syntax": "off" },
  },
  {
    // epFetch's own implementation is the one sanctioned home for a bare fetch() (#529).
    files: ["src/lib/http.ts"],
    rules: { "no-restricted-globals": "off" },
  },
);
