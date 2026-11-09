// Themes: which theme the page shows, light or dark, and the theme's animated effect (docs/themes.md). A theme is a folder
// under static/themes/: a palette (theme.json), and optionally a skin (skin.css) and an effect (effect.js). The server
// lists them on <html data-looks>, and theme.js has already put this browser's choice on <html data-look> (and data-theme)
// before the first paint, and linked that theme's stylesheet (its token blocks) and skin. tokens.css and those sheets read
// the two attributes. Another theme's sheets are linked when it is picked, and the page changes to it once they have
// loaded: no moment in the new theme without its styles. The old sheets are then unlinked.
//
// This browser's choices (localStorage): `penguwatch.look` (a theme id; none: the server's default, the
// Display > Theme setting, display.theme), `penguwatch.mode` (light or dark; none: the system's) and `penguwatch.effects`
// ("on" or "off"; none: the Display > Theme animation setting, display.effects, on <html data-effects-default>). The
// header's theme button opens a menu with all three (fillThemeMenu, called by chrome.js when it opens). A theme that has
// one mode forces it.
//
// Every change goes through `changed()`: the browser's theme-color follows --bg, the effect is started or stopped, and a
// `themechange` event tells modules that draw something that reads the tokens only once.
//
// Effects. A theme's effect.js is an ES module whose default export is {start(layer, ctx) -> stop}. `layer` is #fx-layer,
// a fixed element behind the whole page (z-index -1, pointer-events none: page.css), so the effect is never drawn over a
// poster, a chip or text. ctx gives `tokens()` (the current tokens by name, read when asked), `fps` (the cap, MAX_FPS) and
// `onFrame(cb, fps)`: the engine's one frame loop, which the engine owns, not the effect:
//   - it never starts while the system asks for reduced motion, and stops when it starts asking;
//   - it is paused while the tab is hidden;
//   - it runs at most MAX_FPS frames a second (or the fewer the effect asks for), and halves its rate when the effect's
//     frames take longer than BUDGET_MS on average, down to MIN_FPS; past that the effect is stopped;
//   - it runs only while the theme has an effect and effects are on (this browser's choice, else the setting).
// The layer says what runs: data-fx (the theme id), data-state (running, paused, or over-budget once given up) and
// data-fps (the rate now); its `fxFrames` property counts the frames drawn (for the tests).
import { registerAction } from "./dispatch.js";
import { esc } from "./util.js";

const LOOK_KEY = "penguwatch.look";
const MODE_KEY = "penguwatch.mode";
const FX_KEY = "penguwatch.effects";
export const MAX_FPS = 12;
const MIN_FPS = 1;
const BUDGET_MS = 4; // an effect's own drawing time per frame, averaged over the last frames
const BUDGET_FRAMES = 6; // frames measured before the average counts

const root = document.documentElement;
const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

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
// The theme's stylesheet (`css` in the page list) and skin (`skin`), linked where theme.js linked the first ones: after the
// page's shared stylesheets, the skin after the token blocks. linkSheet returns a Promise that settles once the sheet has
// loaded (or failed: the theme then shows with the standard styles), or null when there is nothing to wait for: no sheet,
// or one already there (theme.js's links have settled before any module runs: module scripts wait for the stylesheets in
// <head>).
const MARKS = { css: "lookCss", skin: "lookSkin" }; // the <link>'s data-look-css / data-look-skin: the theme id
const sheets = (key) => [...document.querySelectorAll(key === "css" ? "link[data-look-css]" : "link[data-look-skin]")];
const pending = new WeakMap(); // a link added here -> the Promise of its load, until it settles
// Only our own files (the same rule as theme.js, static/themes/ under the tree hash): a foreign URL is never linked.
const ours = (url) => typeof url === "string" && /^\/static\/v\/[\w-]+\/themes\/[\w-]+\/[\w-]+\.css$/.test(url);
// and an effect only from its own place in a theme folder: never another script, never another site
const ourEffect = (url) => typeof url === "string" && /^\/static\/v\/[\w-]+\/themes\/[\w-]+\/effect\.js$/.test(url);
function linkSheet(look, key) {
  if (!look || !ours(look[key])) return null;
  const there = sheets(key).find((l) => l.dataset[MARKS[key]] === look.id);
  if (there) return pending.get(there) || null;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = look[key];
  link.dataset[MARKS[key]] = look.id;
  const after = (key === "skin" && sheets("css").pop()) || sheets(key).pop() || sheets("css").pop()
    || document.querySelector('script[src$="/theme.js"]');
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
  const waits = Object.keys(MARKS).map((key) => linkSheet(look, key)).filter(Boolean);
  if (waits.length) Promise.all(waits).then(() => { if (seq === applySeq) show(look); });
  else show(look);
}

function show(look) {
  root.dataset.look = look ? look.id : "default";
  const mode = forcedMode(look) || read(MODE_KEY);
  if (mode === "light" || mode === "dark") root.dataset.theme = mode; else delete root.dataset.theme;
  for (const key of Object.keys(MARKS)) {
    sheets(key).forEach((l) => { if (l.dataset[MARKS[key]] !== root.dataset.look) l.remove(); }); // only the theme in use
  }
  changed();
}

function changed() {
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = getComputedStyle(root).getPropertyValue("--bg").trim() || meta.content;
  runEffect();
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

// ---------- the effect ----------
const layer = () => document.getElementById("fx-layer");
const frames = new Map(); // a frame callback -> the frames a second its effect asked for
let rate = MAX_FPS; // frames a second now: what the effect asked for, lowered when its frames run long
let timer = 0;
let raf = 0;
let last = 0; // the time of the last frame (0: none since the loop was paused)
let spent = 0; // the effect's average drawing time per frame, ms
let measured = 0; // frames in that average
let fx = null; // the effect running: { url, stop }
let fxSeq = 0; // bumped by every stop: an effect still loading for an older start is dropped
let gaveUp = ""; // the effect stopped for running over its budget: not started again until effects are switched

// "on" or "off": this browser's choice, else the server's (display.effects)
export const effectsOn = () => (read(FX_KEY) ? read(FX_KEY) === "on" : root.dataset.effectsDefault !== "off");
const effectWanted = (look) => !!look && ourEffect(look.effect) && effectsOn() && !reducedMotion.matches && look.effect !== gaveUp;

function state(name) {
  const el = layer();
  if (el) { el.dataset.state = name; el.dataset.fps = String(rate); }
}

// One frame at most every 1000 / rate ms: a timer, then the browser's next frame (paused while the tab is hidden).
function schedule() {
  if (timer || raf || !frames.size || document.hidden) return;
  const wait = Math.max(0, 1000 / rate - (performance.now() - last));
  timer = setTimeout(() => { timer = 0; raf = requestAnimationFrame(tick); }, wait);
}

function pause() {
  clearTimeout(timer);
  cancelAnimationFrame(raf);
  timer = 0;
  raf = 0;
  last = 0;
}

function tick(now) {
  raf = 0;
  const dt = last ? now - last : 0;
  last = now;
  const t0 = performance.now();
  try {
    frames.forEach((_, cb) => cb(now, dt));
  } catch (e) {
    console.error(`The theme's effect failed and was stopped: ${e.message}`);
    stopEffect();
    return;
  }
  const el = layer();
  if (el) el.fxFrames = (el.fxFrames || 0) + 1;
  if (overBudget(performance.now() - t0)) return;
  schedule();
}

// The frame budget: the effect's drawing time, averaged; over BUDGET_MS the rate halves, and below MIN_FPS the effect is
// stopped (true) and not started again until effects are switched off and on.
function overBudget(ms) {
  measured += 1;
  spent += (ms - spent) / Math.min(measured, BUDGET_FRAMES);
  if (measured < BUDGET_FRAMES || spent <= BUDGET_MS) return false;
  if (rate > MIN_FPS) {
    rate = Math.max(MIN_FPS, Math.floor(rate / 2));
    measured = 0;
    spent = 0;
    state("running");
    return false;
  }
  gaveUp = fx ? fx.url : "";
  stopEffect();
  state("over-budget");
  return true;
}

function stopEffect() {
  fxSeq += 1;
  pause();
  frames.clear();
  const running = fx;
  fx = null;
  if (running && typeof running.stop === "function") {
    try { running.stop(); } catch (e) { console.error(`The theme's effect did not stop cleanly: ${e.message}`); }
  }
  const el = layer();
  if (!el) return;
  el.replaceChildren(); // whatever the effect left behind
  delete el.dataset.fx;
  delete el.dataset.state;
  delete el.dataset.fps;
}

// Every token by name, read when asked (tokens()["--accent"]: the accent as the page has it now): an effect draws in the
// theme's colours.
const tokenView = new Proxy({}, {
  get: (_, name) => (typeof name === "string" && name.startsWith("--") ? getComputedStyle(root).getPropertyValue(name).trim() : undefined),
});

async function runEffect() {
  const look = lookInfo(currentLook());
  const url = effectWanted(look) ? look.effect : "";
  if (fx && fx.url === url) return;
  stopEffect();
  const el = layer();
  if (!url || !el) return;
  const seq = fxSeq;
  let mod;
  try {
    mod = await import(url);
  } catch (e) {
    console.error(`The theme's effect could not be loaded: ${e.message}`);
    return;
  }
  if (seq !== fxSeq || !mod.default || typeof mod.default.start !== "function") return;
  rate = MAX_FPS;
  spent = 0;
  measured = 0;
  const ctx = {
    fps: MAX_FPS,
    tokens: () => tokenView,
    onFrame: (cb, fps = MAX_FPS) => {
      frames.set(cb, Math.min(MAX_FPS, Math.max(MIN_FPS, Number(fps) || MAX_FPS)));
      rate = Math.min(rate, Math.max(...frames.values()));
      schedule();
      return () => { frames.delete(cb); };
    },
  };
  fx = { url, stop: null };
  try {
    fx.stop = mod.default.start(el, ctx);
  } catch (e) {
    console.error(`The theme's effect could not start: ${e.message}`);
    stopEffect();
    return;
  }
  el.dataset.fx = look.id;
  state(document.hidden ? "paused" : "running");
}

export function setEffects(choice) {
  write(FX_KEY, choice === "on" || choice === "off" ? choice : "");
  gaveUp = "";
  runEffect();
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) pause(); else schedule();
  if (fx) state(document.hidden ? "paused" : "running");
});
reducedMotion.addEventListener("change", () => runEffect());

// ---------- the header's theme menu ----------
const item = (checked, action, arg, text, extra = "", role = "menuitemradio") => `<button type="button" role="${role}" aria-checked="${checked}" data-action="${action}" data-arg="${esc(arg)}"${extra}><span class="tm-mark" aria-hidden="true"></span><span class="tm-text">${text}</span></button>`;
const modeWords = (t) => (t.modes.length === 1 ? `<span class="tm-note">${t.modes[0]} only</span>` : "");

export function fillThemeMenu(menu) {
  const mine = lookInfo(read(LOOK_KEY)) ? read(LOOK_KEY) : "";
  const look = lookInfo(currentLook());
  const def = lookInfo(defaultLook());
  const forced = forcedMode(look);
  const mode = read(MODE_KEY);
  const parts = ['<div class="tm-h" role="presentation">Theme for this browser</div>',
    item(!mine, "pick-look", "", `Use the default <span class="tm-note">${esc(def ? def.name : "PenguWatch")}</span>`)];
  // two small groups, each by name as the page lists them: the palettes (colours only) and the skins (shapes and an effect)
  [["Palettes", looks.filter((t) => !t.skin)], ["Skins", looks.filter((t) => t.skin)]].forEach(([words, list]) => {
    if (list.length) parts.push(`<div class="tm-sub" role="presentation">${words}</div>`);
    list.forEach((t) => parts.push(item(mine === t.id, "pick-look", t.id, `${esc(t.name)}${modeWords(t)}`)));
  });
  parts.push('<hr><div class="tm-h" role="presentation">Light or dark</div>');
  const off = forced ? " disabled" : "";
  [["", "Like the system"], ["light", "Light"], ["dark", "Dark"]].forEach(([m, words]) => {
    parts.push(item(forced ? m === forced : mode === m, "pick-mode", m, words, off));
  });
  if (forced) parts.push(`<p class="tm-why">${esc(look.name)} has a ${forced} look only.</p>`);
  // the effect switch: kept in this browser; what it changes (the theme has none, reduced motion) is said under it
  const on = effectsOn();
  parts.push("<hr>", item(on, "toggle-effects", on ? "off" : "on", `Animation: ${on ? "on" : "off"}`, "", "menuitemcheckbox"));
  if (reducedMotion.matches) parts.push('<p class="tm-why">Not shown: the system asks for reduced motion.</p>');
  else if (!look || !look.effect) parts.push(`<p class="tm-why">${esc(look ? look.name : "This theme")} has no animation.</p>`);
  menu.innerHTML = parts.join("");
}

registerAction("pick-look", (el) => setLook(el.dataset.arg));
registerAction("pick-mode", (el) => setMode(el.dataset.arg));
registerAction("toggle-effects", (el) => setEffects(el.dataset.arg));

// At start (chrome.js): theme.js has set the attributes; this reads them once for the theme-color, the sheets and the
// effect.
export function initThemes() {
  apply();
}
