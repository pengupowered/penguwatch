// Palettes only: no effects, card views, card colours or chart style.
//
// Themes: which palette the page shows, and light or dark (docs/themes.md). A theme is a folder under static/themes/; the
// server lists them on <html data-looks>, and theme.js has already put this browser's choice on <html data-look> (and
// data-theme) before the first paint, and linked that theme's stylesheet (its token blocks). tokens.css and that
// stylesheet read those two attributes. Another theme's stylesheet is linked when it is picked, and the page changes to it
// once the sheet has loaded: no moment in the new theme without its styles. The old sheet is then unlinked.
//
// This browser's choices (localStorage): `penguwatch.look` (a theme id; none: the server's default, the
// Display > Theme setting, display.theme) and `penguwatch.mode` (light or dark; none: the system's). The header's theme button opens a menu with
// both (fillThemeMenu, called by chrome.js when it opens). A theme that has one mode forces it.
//
// Every change goes through `changed()`: the browser's theme-color follows --bg, and a `themechange` event tells modules
// that draw something that reads the tokens only once.
import { registerAction } from "./dispatch.js";
import { esc } from "./util.js";

const LOOK_KEY = "penguwatch.look";
const MODE_KEY = "penguwatch.mode";

const root = document.documentElement;
const systemDark = window.matchMedia("(prefers-color-scheme: dark)");

function read(key) {
  try { return localStorage.getItem(key) || ""; } catch { return ""; }
}
function write(key, value) {
  try {
    if (value) localStorage.setItem(key, value); else localStorage.removeItem(key);
  } catch {
    // not kept; applies until the page is closed
  }
}

// ---------- the themes ----------
let looks = [];
try { looks = JSON.parse(root.dataset.looks || "[]"); } catch { looks = []; }

const lookInfo = (id) => looks.find((t) => t.id === id) || null;
const currentLook = () => root.dataset.look || "default";
const defaultLook = () => root.dataset.lookDefault || "default";
export const isDark = () => (root.dataset.theme ? root.dataset.theme === "dark" : systemDark.matches);
const forcedMode = (look) => (look && look.modes.length === 1 ? look.modes[0] : "");

// ---------- applying ----------
// The theme's stylesheet (`css` in the page list), linked where theme.js linked the first one: after the page's shared
// stylesheets. Returns a Promise that settles once it has loaded (or failed: the theme then shows with the standard
// styles), or null when there is nothing to wait for: no stylesheet, or one already there (theme.js's link has settled
// before any module runs: module scripts wait for the stylesheets in <head>).
const sheets = () => [...document.querySelectorAll("link[data-look-css]")];
const pending = new WeakMap(); // a link added here -> the Promise of its load, until it settles
// Only our own files (the same rule as theme.js, static/themes/ under the tree hash): a foreign URL is never linked.
const ours = (url) => typeof url === "string" && /^\/static\/v\/[\w-]+\/themes\/[\w-]+\/[\w-]+\.css$/.test(url);
function linkSheet(look) {
  if (!look || !ours(look.css)) return null;
  const there = sheets().find((l) => l.dataset.lookCss === look.id);
  if (there) return pending.get(there) || null;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = look.css;
  link.dataset.lookCss = look.id;
  const after = sheets().pop() || document.querySelector('script[src$="/theme.js"]');
  if (after) after.after(link); else document.head.append(link);
  const loaded = new Promise((resolve) => {
    const done = () => { pending.delete(link); resolve(); };
    link.addEventListener("load", done, { once: true });
    link.addEventListener("error", done, { once: true });
  });
  pending.set(link, loaded);
  return loaded;
}

// The theme the page shows: this browser's choice, else the server's default.
const chosenLook = () => lookInfo(read(LOOK_KEY)) || lookInfo(defaultLook()) || lookInfo("default");

let applySeq = 0;
function apply() {
  const look = chosenLook();
  const seq = ++applySeq;
  const wait = linkSheet(look);
  if (wait) wait.then(() => { if (seq === applySeq) show(look); });
  else show(look);
}

function show(look) {
  root.dataset.look = look ? look.id : "default";
  const mode = forcedMode(look) || read(MODE_KEY);
  if (mode === "light" || mode === "dark") root.dataset.theme = mode; else delete root.dataset.theme;
  sheets().forEach((l) => { if (l.dataset.lookCss !== root.dataset.look) l.remove(); }); // only the theme in use
  changed();
}

function changed() {
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = getComputedStyle(root).getPropertyValue("--bg").trim() || meta.content;
  document.dispatchEvent(new CustomEvent("themechange"));
}

export function setLook(id) {
  write(LOOK_KEY, id && lookInfo(id) ? id : "");
  apply();
}

export function setMode(mode) {
  write(MODE_KEY, mode === "light" || mode === "dark" ? mode : "");
  apply();
}

systemDark.addEventListener("change", () => { if (!root.dataset.theme) changed(); });

// ---------- the header's theme menu ----------
const item = (checked, action, arg, text, extra = "") => `<button type="button" role="menuitemradio" aria-checked="${checked}" data-action="${action}" data-arg="${esc(arg)}"${extra}><span class="tm-mark" aria-hidden="true"></span><span class="tm-text">${text}</span></button>`;
const modeWords = (t) => (t.modes.length === 1 ? `<span class="tm-note">${t.modes[0]} only</span>` : "");

export function fillThemeMenu(menu) {
  const mine = lookInfo(read(LOOK_KEY)) ? read(LOOK_KEY) : "";
  const look = lookInfo(currentLook());
  const def = lookInfo(defaultLook());
  const forced = forcedMode(look);
  const mode = read(MODE_KEY);
  const parts = ['<div class="tm-h" role="presentation">Theme for this browser</div>',
    item(!mine, "pick-look", "", `Use the default <span class="tm-note">${esc(def ? def.name : "PenguWatch")}</span>`)];
  looks.forEach((t) => parts.push(item(mine === t.id, "pick-look", t.id, `${esc(t.name)}${modeWords(t)}`)));
  parts.push('<hr><div class="tm-h" role="presentation">Light or dark</div>');
  const off = forced ? " disabled" : "";
  [["", "Like the system"], ["light", "Light"], ["dark", "Dark"]].forEach(([m, words]) => {
    parts.push(item(forced ? m === forced : mode === m, "pick-mode", m, words, off));
  });
  if (forced) parts.push(`<p class="tm-why">${esc(look.name)} has a ${forced} look only.</p>`);
  menu.innerHTML = parts.join("");
}

registerAction("pick-look", (el) => setLook(el.dataset.arg));
registerAction("pick-mode", (el) => setMode(el.dataset.arg));

// At start (chrome.js): theme.js has set the attributes; this reads them once for the theme-color and the sheet.
export function initThemes() {
  apply();
}
