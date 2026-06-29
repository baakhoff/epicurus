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
      ],
    },
  },
  {
    // The primitives in ui.tsx are the one sanctioned home for the raw <input>/<select>.
    files: ["src/components/ui.tsx"],
    rules: { "no-restricted-syntax": "off" },
  },
);
