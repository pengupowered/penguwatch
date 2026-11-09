// The shared half of the browser smoke test: the browser, the page, and every helper a step uses. Defined once, here.
// smoke.mjs (the runner) calls createContext() and hands the result to each step as its only argument; the step files
// in steps/ destructure what they need from it. A step file never declares a helper of its own at the top level (the
// guard in guards.mjs fails the run if two files do, or if one shadows a name exported here): a helper two files need
// belongs in this file.
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

export const PHONE = { width: 390, height: 844, isMobile: true, hasTouch: true };
export const DESKTOP = { width: 1440, height: 1000 };
// localStorage keys (static/theme.js, js/themes.js): the theme id, the light/dark pick, the animation pick, and the key the
// header button kept the light/dark pick in before themes (theme.js reads it once and removes it)
export const LOOK_KEY = "penguwatch.look";
export const MODE_KEY = "penguwatch.mode";
export const FX_KEY = "penguwatch.effects";
export const OLD_THEME_KEY = "penguwatch.theme";
// The themes of static/themes/ and the modes each has, read from the theme folders themselves (a new folder is tested without
// a line here); the page lists the same (<html data-looks>), a step checks it. Order: the default, then by name, as the page.
const THEMES_DIR = path.join(path.dirname(new URL(import.meta.url).pathname), "..", "..", "src", "penguwatch", "web", "static", "themes");
export const THEME_NAMES = {};
export const THEMES = [["default", ["dark", "light"]], ...fs.readdirSync(THEMES_DIR).filter((id) => id !== "default")
  .map((id) => { const j = JSON.parse(fs.readFileSync(path.join(THEMES_DIR, id, "theme.json"), "utf8")); THEME_NAMES[id] = j.name; return [id, ["dark", "light"].filter((m) => j.modes.includes(m)), j.name]; })
  .sort((a, b) => a[2].localeCompare(b[2]))];
THEME_NAMES.default = "PenguWatch";
// The themes with a skin (skin.css), and whether each also has an effect (effect.js): core.mjs's skin steps run every one
// (docs/themes.md, "The contract for a skin author"). A new skin adds its id here.
export const SKINS = [["amber-crt", { effect: true }], ["cinema", { effect: true }], ["starship-console", { effect: true }], ["synthwave", { effect: true }], ["video-store", { effect: true }]];
// The host name the browser opens the app by (test_ui_smoke.py BROWSER_HOST), mapped to 127.0.0.1 below. Not a local name
// on purpose: a page on 127.0.0.1 or localhost is a secure context, where the Clipboard API, notifications and downloads
// work, which hides what fails for a person who opens http://homeserver:8080 from another machine.
export const BROWSER_HOST = "penguwatch-test";
// The same address on 127.0.0.1, for requests made by node itself (a step's fetch), which cannot resolve BROWSER_HOST.
export const loopback = (url) => url.replace(BROWSER_HOST, "127.0.0.1");
export const SHOTS = process.env.PENGUWATCH_SMOKE_SHOTS === "1";

// Console errors the page makes on purpose (none yet: a step that provokes one adds its pattern here).
const KNOWN_CONSOLE_ERRORS = [];
// Answers the app gives that are known and not what this test is about: [status, URL pattern, why]. Both the HTTP response
// and the console's "Failed to load resource" line for it are let through. Remove an entry when the app stops doing it.
// (None now.)
const KNOWN_RESPONSES = [];
// Elements that stick out sideways and are known, so the check can go on finding new ones: [selector, why]. Remove an
// entry when the app is fixed. (None now.)
const KNOWN_OVERFLOWS = [];
const knownResponse = (status, url) => KNOWN_RESPONSES.some(([s, re]) => s === status && re.test(url));

export async function createContext({ base, outDir, variant }) {
  const require = createRequire(path.join(process.env.PGBROWSER, "package.json"));
  const puppeteer = require("puppeteer-core");
  fs.mkdirSync(outDir, { recursive: true });
  const firefox = process.env.PENGUWATCH_BROWSER === "firefox";
  const executablePath = firefox ? process.env.FIREFOX_EXECUTABLE_PATH : process.env.PUPPETEER_EXECUTABLE_PATH;
  if (!executablePath) {
    console.error(`${firefox ? "FIREFOX_EXECUTABLE_PATH" : "PUPPETEER_EXECUTABLE_PATH"} is not set: point it at the browser's executable (scripts/test-ui.sh does this in the container).`);
    process.exit(2);
  }
  // PENGUWATCH_BROWSER=firefox: Firefox over WebDriver BiDi (see docs/development.md); the default is Chromium over CDP.
  const args = (process.env.PENGUWATCH_BROWSER_ARGS ?? "").split(/\s+/).filter(Boolean);
  const browser = firefox
    ? await puppeteer.launch({ browser: "firefox", protocol: "webDriverBiDi", executablePath, headless: true,
      // network.dns.localDomains: these names resolve to the loopback address (the container has no network and no DNS)
      extraPrefsFirefox: { "network.dns.localDomains": BROWSER_HOST } })
    : await puppeteer.launch({ executablePath, args: [...args, `--host-resolver-rules=MAP ${BROWSER_HOST} 127.0.0.1`], headless: true });
  const page = await browser.newPage();
  await page.setViewport(DESKTOP);
  await page.evaluateOnNewDocument(() => {
    // The page is not a secure context (BROWSER_HOST), so it has no navigator.clipboard, as for a person on the LAN: it
    // copies through a hidden textarea and execCommand("copy"). window.__clip() is what that last copied, taken from the
    // copy event (the system clipboard of a headless browser cannot be read).
    let clip = "";
    document.addEventListener("copy", () => {
      const box = [...document.querySelectorAll("textarea")].find((t) => t.selectionStart !== t.selectionEnd);
      clip = box ? box.value.slice(box.selectionStart, box.selectionEnd) : String(getSelection());
    }, true);
    window.__clip = () => clip;
    window.__csp = [];
    document.addEventListener("securitypolicyviolation", (e) => window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
  });
  const problems = [];
  const dialogs = [];
  // A step that ends the page's sign-in on purpose (expectRefusals(true)) expects 401s until it says otherwise.
  let refusalsExpected = false;
  const expectRefusals = (on) => { refusalsExpected = on; };
  // A step that makes the server answer a status on purpose says which until it says none.
  let statusExpected = [];
  const expectStatus = (...codes) => { statusExpected = codes; };
  const refusal = (text) => (refusalsExpected && /status of 401/.test(text)) || statusExpected.some((c) => text.includes(`status of ${c}`));
  // Firefox reports, as errors the page did not make: a module import that a reload cancelled mid-load (a module that is
  // really missing is an HTTP 404 as well, which is still a problem), and a fetch a reload cancelled: the page's .catch logs
  // the error, which Firefox's console gives as "undefined" (a request that really failed is a requestfailed or an HTTP
  // status, still a problem).
  const FIREFOX_NOISE = [/error loading dynamically imported module/, /^undefined$/];
  const noise = (text) => firefox && FIREFOX_NOISE.some((re) => re.test(text));
  page.on("pageerror", (e) => { if (!noise(e.message)) problems.push(`pageerror: ${e.message}`); });
  page.on("console", async (m) => {
    if (m.type() !== "error") return;
    let text = m.text();
    // Firefox gives an Error argument as "JSHandle@error": read its message
    if (/^JSHandle@/.test(text)) text = (await Promise.all(m.args().map((a) => a.evaluate((v) => (v instanceof Error ? v.message : String(v))).catch(() => "")))).join(" ");
    const status = Number(/status of (\d{3})/.exec(text)?.[1]);
    if (status && knownResponse(status, m.location()?.url ?? "")) return;
    if (!KNOWN_CONSOLE_ERRORS.some((re) => re.test(text)) && !refusal(text) && !noise(text)) problems.push(`console.error: ${text}`);
  });
  // Firefox also reports a request that a reload or navigation cancelled (NS_BINDING_ABORTED); Chromium does not here.
  page.on("requestfailed", (r) => {
    const why = r.failure()?.errorText;
    if (firefox && why === "NS_BINDING_ABORTED") return;
    problems.push(`requestfailed: ${r.url()} ${why}`);
  });
  page.on("response", (r) => {
    if (r.url().startsWith(base) && r.status() >= 400 && !noise(r.url()) && !(refusalsExpected && r.status() === 401) && !statusExpected.includes(r.status()) && !knownResponse(r.status(), r.url())) problems.push(`HTTP ${r.status()}: ${r.url()}`);
  });
  page.on("dialog", (d) => { dialogs.push(`${d.type()}: ${d.message()}`); d.dismiss(); });

  const stats = { steps: [], failed: [], assertions: 0 };
  const report = {}; // a step may leave a note for the final output

  // ---- waiting and reading ----
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const until = (fn, ...a) => page.waitForFunction(fn, { timeout: 15000 }, ...a).catch((e) => {
    throw new Error(`${e.message} while waiting for ${String(fn).slice(0, 160)} with ${JSON.stringify(a)}`);
  });
  // n animation frames: a scroll or resize event the page was sent has been delivered (they fire at the start of a frame)
  const frames = (n = 3) => page.evaluate((k) => new Promise((r) => { const tick = (i) => (i ? requestAnimationFrame(() => tick(i - 1)) : r()); tick(k); }), n);
  // Waits until the layout has stopped moving after a resize, a tab switch or a reload: the page's size has been the same
  // for 8 frames in a row (at most 4 s, then the step carries on and its own checks decide).
  const layoutSettled = () => page.evaluate(() => new Promise((r) => {
    const sig = () => `${innerWidth}x${innerHeight} ${document.documentElement.scrollWidth}x${document.documentElement.scrollHeight}`;
    const t0 = performance.now();
    let last = sig();
    let same = 0;
    const tick = () => {
      const now = sig();
      same = now === last ? same + 1 : 0;
      last = now;
      if (same >= 8 || performance.now() - t0 > 4000) r();
      else requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }));
  const text = (sel) => page.$eval(sel, (e) => e.textContent);
  const count = (sel) => page.$$eval(sel, (els) => els.length);
  const shown = (sel) => page.$eval(sel, (e) => getComputedStyle(e).display !== "none");
  const visible = (sel) => page.$eval(sel, (e) => { const r = e.getBoundingClientRect(); return getComputedStyle(e).display !== "none" && r.width > 0 && r.height > 0; });
  // el.click() instead of a mouse click: panels are re-rendered after data loads, which can detach the target mid-click.
  const click = (sel) => page.evaluate((s) => document.querySelector(s).click(), sel);
  const waitText = (sel, re) => until((s, r) => new RegExp(r).test(document.querySelector(s)?.textContent ?? ""), sel, re.source);
  const waitCount = (sel, n) => until((s, k) => document.querySelectorAll(s).length === k, sel, n);
  // at least n matches (a panel's cards or rows have rendered)
  const waitSome = (sel, n = 1) => until((s, k) => document.querySelectorAll(s).length >= k, sel, n);
  const modalOpen = (id) => page.$eval(`#${id}`, (m) => m.classList.contains("active"));
  const eq = (what, got, want) => { stats.assertions++; if (got !== want) throw new Error(`${what}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`); };
  const ok = (what, cond) => { stats.assertions++; if (!cond) throw new Error(what); };

  // Tab (and Shift+Tab) never reaches the page behind an open dialog.
  const tabStaysIn = async (modal) => {
    for (const shift of [false, true]) {
      if (shift) await page.keyboard.down("Shift");
      let outside = 0;
      for (let i = 0; i < 25; i++) {
        await page.keyboard.press("Tab");
        if (!(await page.evaluate((m) => document.getElementById(m).contains(document.activeElement), modal))) outside++;
      }
      if (shift) await page.keyboard.up("Shift");
      eq(`${modal}: ${shift ? "Shift+Tab" : "Tab"} stops outside the dialog`, outside, 0);
    }
  };

  // A dialog's keyboard: it opens with focus inside it, Tab stays in it, Escape closes it, and focus goes back to the
  // control that opened it.
  const dialogKeys = async (opener, modal) => {
    await page.$eval(opener, (e) => e.focus());
    await click(opener);
    await until((m) => document.getElementById(m).classList.contains("active"), modal);
    await until((m) => document.getElementById(m).contains(document.activeElement), modal);
    await tabStaysIn(modal);
    await page.keyboard.press("Escape");
    await until((m) => !document.getElementById(m).classList.contains("active"), modal);
    eq(`${modal}: focus is back on its opener`, await page.evaluate((o) => document.activeElement === document.querySelector(o), opener), true);
  };

  // The old wordings are gone from the page, wherever they were (the page's HTML, not only its visible text).
  const noOldLabels = async () => {
    const found = await page.evaluate(() => (document.documentElement.innerHTML.match(/\bOur rating\b|On our services|Should We Watch/g) ?? []).join(", "));
    eq("old wordings in the page", found, "");
  };

  // ---- the page ----
  // The tabs the server found: [{ id, route, tab }] in page order, from the nav buttons (templates/index.html).
  const tabsOf = () => page.$$eval(".nav-btn[data-tab]", (els) => els.map((e) => ({ id: e.id.replace(/^tab-/, ""), route: e.dataset.route, tab: e.dataset.tab })));
  const panelOrder = () => page.$$eval("[data-panel]", (els) => els.map((e) => e.dataset.panel));
  // The page is up: the tabs are mounted (one is active) and the header's library status has answered.
  const pageReady = () => until(() => document.querySelector(".nav-btn.active") && !/^Checking/.test(document.getElementById("library-badge-text")?.textContent ?? "Checking"));
  // Opens a tab by its route, the way a bookmark does, and waits until its pane is the active one.
  const goTab = async (route) => {
    await page.evaluate((r) => { location.hash = "#/" + r; }, route);
    await until((r) => document.querySelector(`.nav-btn.active[data-route="${r}"]`) && document.querySelector(".tab-pane.active"), route);
    await layoutSettled();
  };
  // Puppeteer reloads the page when the mobile emulation switches on or off, so dialogs are opened after each switch.
  const phone = async (fn) => {
    await page.setViewport(PHONE);
    try { await pageReady(); await fn(); } finally { await page.setViewport(DESKTOP); await pageReady(); }
  };
  // What sticks out sideways: the page is wider than its window, or an element of the visible page, header or tab bar
  // reaches past the window's edges (fixed elements, menus, canvases and whatever sits inside a box that scrolls sideways
  // on its own, like a wide table in a .table-wrap, are left out). A page that scrolls sideways has something in this
  // list. [] means all fits.
  const sticksOut = () => page.evaluate((list) => {
    const known = list.length ? list : [":not(*)"]; // [] would make an empty selector
    const w = document.documentElement.clientWidth;
    const out = [];
    // the page's own width is allowed to be as wide as a known element makes it, no wider
    const knownRight = Math.max(w, ...[...document.querySelectorAll(known.join(","))].map((e) => e.getBoundingClientRect().right));
    if (document.documentElement.scrollWidth > knownRight + 1) out.push(`page scrollWidth ${document.documentElement.scrollWidth} > ${w}`);
    document.querySelectorAll(".tab-pane.active *, .topbar *").forEach((e) => {
      if (e.closest(`[hidden], .menu, canvas, .skip, .sr-only, ${known.join(",")}`)) return;
      for (let a = e.parentElement; a && a !== document.body; a = a.parentElement) if (/auto|scroll/.test(getComputedStyle(a).overflowX)) return;
      const r = e.getBoundingClientRect();
      if (r.width && (r.right > w + 1 || r.left < -1) && getComputedStyle(e).position !== "fixed") out.push(`${e.tagName.toLowerCase()}.${String(e.className.baseVal ?? e.className).split(" ")[0]} ${Math.round(r.left)}..${Math.round(r.right)}`);
    });
    return out.slice(0, 6);
  }, KNOWN_OVERFLOWS.map(([selector]) => selector));
  const noSideScroll = async () => (await sticksOut()).length === 0;

  // ---- theme and light or dark, through the header's theme menu (so the page re-themes as it does for a person) ----
  const systemTheme = await page.evaluate(() => (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  // "light" or "dark": what shows now (the pick on <html data-theme>, else the system's)
  const themeNow = () => page.evaluate(() => document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  const lookNow = () => page.evaluate(() => document.documentElement.dataset.look);
  const stored = (key) => page.evaluate((k) => { try { return localStorage.getItem(k); } catch { return null; } }, key);
  const storedMode = () => stored(MODE_KEY);
  const storedLook = () => stored(LOOK_KEY);
  const menuOpen = () => page.$eval("#theme-menu", (m) => !m.hidden);
  // opens the menu with the header button; picks an item (action pick-look or pick-mode, data-arg) and waits for it to close
  const pickInMenu = async (action, arg) => {
    await click("#btn-theme");
    await until(() => !document.getElementById("theme-menu").hidden);
    await click(`#theme-menu [data-action=${action}][data-arg="${arg}"]`);
    await until(() => document.getElementById("theme-menu").hidden);
  };
  // pickMode("light" | "dark" | ""): "" is "Like the system"; done once <html data-theme> shows it
  const pickMode = async (mode) => {
    await pickInMenu("pick-mode", mode);
    await until((m) => (m ? document.documentElement.dataset.theme === m : !document.documentElement.dataset.theme), mode);
  };
  // pickLook(id): "" is "Use the default" (the server's); done once the theme is on the page (its stylesheet has loaded)
  const pickLook = async (id) => {
    await pickInMenu("pick-look", id);
    await until((want) => document.documentElement.dataset.look === (want || document.documentElement.dataset.lookDefault), id);
  };
  const flipMode = async () => pickMode((await themeNow()) === "dark" ? "light" : "dark");
  // "light" or "dark", or null for the system's
  const setTheme = async (want) => {
    const now = await themeNow();
    if (want ? now !== want : await page.evaluate(() => !!document.documentElement.dataset.theme)) await pickMode(want || "");
  };
  // the value of a token on <html>, as the page computes it (lower case, trimmed)
  const tokenNow = (name) => page.evaluate((n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim().toLowerCase(), name);
  // the effect layer now (js/themes.js): the theme whose effect runs (""), its state (running, paused, over-budget or ""),
  // the frames it has drawn, what it holds, and its rate
  const fxNow = () => page.evaluate(() => {
    const l = document.getElementById("fx-layer");
    return { fx: l.dataset.fx ?? "", state: l.dataset.state ?? "", frames: l.fxFrames ?? 0, kids: l.childElementCount, fps: Number(l.dataset.fps ?? 0) };
  });
  // the menu opened and read: [{ action, arg, checked, disabled, text }]; closed again with Escape
  const readMenu = async () => {
    await click("#btn-theme");
    await until(() => !document.getElementById("theme-menu").hidden);
    const items = await page.$$eval("#theme-menu [role=menuitemradio]", (bs) => bs.map((b) => ({ action: b.dataset.action, arg: b.dataset.arg, checked: b.getAttribute("aria-checked") === "true", disabled: b.disabled, text: b.textContent.trim() })));
    await page.keyboard.press("Escape");
    await until(() => document.getElementById("theme-menu").hidden);
    return items;
  };

  // ---- screenshots ----
  const shot = (name, opts = {}) => page.screenshot({ path: path.join(outDir, `ps-${name}.png`), ...opts }); // the extra set (PENGUWATCH_SMOKE_SHOTS=1)
  const snap = (file, opts = {}) => page.screenshot({ path: path.join(outDir, file), ...opts }); // a named file in outDir

  // ---- the access token's sign-in page (the `token` variant; PENGUWATCH_SMOKE_TOKEN is the token the app has) ----
  // Signs `p` (this page, or a page of another browser context) in the way a person does: open the dashboard, get the
  // sign-in page, type the token, land on the dashboard.
  const signInWithToken = async (p = page) => {
    await p.goto(base + "/", { waitUntil: "load" });
    if (new URL(p.url()).pathname !== "/login") throw new Error(`expected the sign-in page, got ${p.url()}`);
    await p.type("input[name=token]", process.env.PENGUWATCH_SMOKE_TOKEN ?? "");
    await Promise.all([p.waitForNavigation({ waitUntil: "load" }), p.click("button[type=submit]")]);
    if (new URL(p.url()).pathname !== "/") throw new Error(`the sign-in did not reach the dashboard: ${p.url()}`);
    if (await p.evaluate(() => window.isSecureContext)) throw new Error("the page is a secure context: the run must open the app by BROWSER_HOST");
  };

  // ---- the page between step files: a known state (desktop, system theme, nothing stored, a fresh page load) ----
  async function resetPage() {
    if (!firefox) await page.emulateMediaFeatures([]); // CDP only
    await page.setViewport(DESKTOP);
    await page.evaluate(() => { localStorage.clear(); sessionStorage.clear(); location.hash = "#/"; });
    await page.reload({ waitUntil: "load" });
    await pageReady();
  }

  // ---- a step: runs fn(ctx); a failure is recorded with a screenshot ----
  async function step(name, fn) {
    try {
      await fn(ctx);
      stats.steps.push(name);
      return true;
    } catch (e) {
      stats.failed.push({ step: name, error: String(e.message ?? e) });
      await page.screenshot({ path: path.join(outDir, stats.failed.length === 1 ? "failure.png" : `failure-${stats.failed.length}.png`) }).catch(() => {});
      return false;
    }
  }

  const ctx = {
    browser, firefox, page, base, outDir, variant, problems, dialogs, stats, report,
    PHONE, DESKTOP, SHOTS, LOOK_KEY, MODE_KEY, FX_KEY, OLD_THEME_KEY, THEMES, THEME_NAMES, SKINS, BROWSER_HOST, systemTheme, fs, path, loopback,
    sleep, frames, layoutSettled, until, text, count, shown, visible, click, waitText, waitCount, waitSome, modalOpen, eq, ok, dialogKeys, tabStaysIn,
    noOldLabels, tabsOf, panelOrder, pageReady, goTab, phone, sticksOut, noSideScroll, themeNow, lookNow, stored, storedMode, storedLook, menuOpen, pickInMenu, pickMode, pickLook, flipMode, setTheme, tokenNow, readMenu, fxNow,
    shot, snap, signInWithToken, expectRefusals, expectStatus, resetPage, step,
  };
  return ctx;
}
