// ESLint flat config for the dashboard scripts (src/penguwatch/web/static: js/, panels/, dialogs/ and themes/*/effect.js as
// modules, theme.js as a classic script) and the browser test (tests/ui/*.mjs, steps/). ESLint and `globals` are not
// project dependencies: they are pinned in tests/ui/runner/package-lock.json and come from the directory in $PGBROWSER
// (scripts/test-ui.sh). Run it with
//   node $PGBROWSER/node_modules/eslint/bin/eslint.js -c tests/ui/eslint.config.mjs src/penguwatch/web/static/js
// `no-undef` is the point: module scripts are strict, so an implicit global or a forgotten import must fail here.
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(path.join(process.env.PGBROWSER, "package.json"));
const globals = require("globals");

const rules = {
  "no-undef": "error",
  "no-unused-vars": ["warn", { args: "none", caughtErrors: "none" }],
  "no-redeclare": "error",
  "no-const-assign": "error",
};

export default [
  {
    // the browser test itself (runner, lib, step files): Node, with page.evaluate callbacks that run in the browser
    files: ["**/*.mjs"],
    languageOptions: { ecmaVersion: 2022, sourceType: "module", globals: { ...globals.node, ...globals.browser } },
    rules,
  },
  {
    // the dashboard's modules
    files: ["**/*.js"],
    languageOptions: { ecmaVersion: 2022, sourceType: "module", globals: { ...globals.browser } },
    rules: { ...rules, "no-import-assign": "error" },
  },
  {
    // theme.js is a classic script in <head> (a module would run only after parsing): `var`, no import or export
    files: ["**/theme.js"],
    languageOptions: { ecmaVersion: 2022, sourceType: "script", globals: { ...globals.browser } },
    rules,
  },
];
