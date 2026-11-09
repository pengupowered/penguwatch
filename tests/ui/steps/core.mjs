// The page as a whole: header and tabs, every tab at desktop and phone width, the theme menu and every theme, every skin
// and its effect within the engine's limits, the icons.
export const subject = "core";
export const variants = ["demo", "token"];

// Opens every tab (by its route, as a bookmark does) and reports what is wrong with each: a console or page error it
// caused, an empty heading, something sticking out sideways.
const visitTabs = async ({ tabsOf, goTab, page, problems, sleep, sticksOut }) => {
  const wrong = [];
  for (const t of await tabsOf()) {
    const before = problems.length;
    await goTab(t.route);
    await sleep(300); // the tab's own requests and a late console error
    const head = await page.$eval(".tab-pane.active h1", (h) => h.textContent.trim()).catch(() => "");
    if (!head) wrong.push(`${t.id}: no heading`);
    if (await page.$eval(".tab-pane.active", (p) => p.id) !== t.tab) wrong.push(`${t.id}: the pane shown is not ${t.tab}`);
    const out = await sticksOut();
    if (out.length) wrong.push(`${t.id}: sticks out: ${out.join("; ")}`);
    if (problems.length > before) wrong.push(`${t.id}: ${problems.slice(before).join("; ")}`);
  }
  return wrong.join(" | ");
};

// Every theme in each of its modes, picked in the header menu: what the page shows, what the menu says, what a reload
// brings back. The expected colours are the theme.json files' (served under /static/themes/); the default theme's are
// tokens.css's. `colours` ends the run of one width: one theme and mode must not look like another.
const DEFAULT_COLOURS = { dark: { "--bg": "#0a0e13", "--surface-1": "#10151c", "--ink-1": "#edf1f5", "--accent": "#5aa2ff" }, light: { "--bg": "#f1f4f7", "--surface-1": "#ffffff", "--ink-1": "#0f1720", "--accent": "#1a64c4" } };
const rgbOf = (hex) => `rgb(${[1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)).join(", ")})`;
const checkLooks = async ({ THEMES, eq, goTab, ok, page, pageReady, pickLook, pickMode, problems, readMenu, sticksOut, storedLook, storedMode, tokenNow }) => {
  const seen = new Map();
  const before = problems.length;
  const show = (id, mode) => `${id} ${mode} at ${page.viewport().width}`;
  const snapshot = () => page.evaluate(() => ({
    look: document.documentElement.dataset.look, mode: document.documentElement.dataset.theme,
    scheme: getComputedStyle(document.documentElement).colorScheme, body: getComputedStyle(document.body).backgroundColor,
    meta: document.querySelector('meta[name="theme-color"]').content.toLowerCase(),
    sheets: [...document.querySelectorAll("link[data-look-css]")].map((l) => l.dataset.lookCss).join(),
    wide: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
  }));
  // what <html> carries, and its --bg, at the moment <body> starts: the first paint (static/theme.js has run by then; the
  // menu's script has not, and would hide a boot script that ignores the stored choice)
  const { identifier } = await page.evaluateOnNewDocument(() => {
    new MutationObserver((_, obs) => {
      if (!document.body) return;
      const root = document.documentElement;
      window.__atBody = [root.dataset.look, root.dataset.theme ?? "", getComputedStyle(root).getPropertyValue("--bg").trim().toLowerCase(), document.querySelectorAll("link[data-look-css]").length].join("|");
      obs.disconnect();
    }).observe(document, { childList: true, subtree: true });
  });
  for (const [id, modes] of THEMES) {
    const own = id === "default" ? null : await page.evaluate(async (i) => (await (await fetch(`/static/themes/${i}/theme.json`)).json()).tokens, id);
    await pickLook(id);
    for (const mode of modes) {
      if (modes.length > 1) await pickMode(mode);
      const what = show(id, mode);
      const want = (id === "default" ? DEFAULT_COLOURS : own)[mode];
      const check = async (stage) => {
        if (stage === "after a reload") eq(`${what} ${stage}: on <html> with its colours when the body starts`, await page.evaluate(() => window.__atBody), `${id}|${mode}|${want["--bg"].toLowerCase()}|${id === "default" ? 0 : 1}`);
        const now = await snapshot();
        eq(`${what} ${stage}: <html data-look>`, now.look, id);
        eq(`${what} ${stage}: <html data-theme>`, now.mode, mode);
        eq(`${what} ${stage}: color-scheme`, now.scheme, mode);
        for (const token of Object.keys(DEFAULT_COLOURS.dark)) eq(`${what} ${stage}: ${token}`, await tokenNow(token), want[token].toLowerCase());
        eq(`${what} ${stage}: the body is painted with --bg`, now.body, rgbOf(want["--bg"]));
        eq(`${what} ${stage}: theme-color follows --bg`, now.meta, want["--bg"].toLowerCase());
        eq(`${what} ${stage}: only this theme's stylesheet`, now.sheets, id === "default" ? "" : id);
        if (id !== "default" && !want["--poster-bg"]) eq(`${what} ${stage}: --poster-bg is made from --surface-3`, await tokenNow("--poster-bg"), await tokenNow("--surface-3"));
        ok(`${what} ${stage}: no sideways scroll`, !now.wide);
        eq(`${what} ${stage}: nothing sticks out`, (await sticksOut()).join("; "), "");
      };
      await check("after the pick");
      const key = ["--bg", "--surface-1", "--ink-1"].map((t) => want[t]).join("|");
      ok(`${what} looks like ${seen.get(key)}`, !seen.has(key));
      seen.set(key, what);
      // the menu shows the choice
      const items = await readMenu();
      eq(`${what}: the menu's checked items`, items.filter((i) => i.checked).map((i) => `${i.action}:${i.arg}`).join(), `pick-look:${id},pick-mode:${mode}`);
      eq(`${what}: kept (${id}, ${modes.length > 1 ? mode : "forced"})`, [await storedLook(), modes.length > 1 ? await storedMode() : mode].join(), `${id},${mode}`);
      // and a reload brings it back
      await page.reload({ waitUntil: "load" });
      await pageReady();
      await check("after a reload");
      // the History tab (its charts and bars are drawn from tokens only) under the same look
      const home = await page.evaluate(() => location.hash);
      await goTab("history");
      eq(`${what}: History, nothing sticks out`, (await sticksOut()).join("; "), "");
      ok(`${what}: History draws its bars`, await page.$$eval("#history-series path.hc-bar", (e) => e.length) > 0);
      await page.evaluate((h) => { location.hash = h; }, home || "#/upnext");
      await pageReady();
    }
  }
  await page.removeScriptToEvaluateOnNewDocument(identifier);
  ok(`${before === problems.length ? "no" : "new"} page or console errors`, problems.length === before);
  eq("every combination looked different", seen.size, THEMES.reduce((n, [, modes]) => n + modes.length, 0));
  // leave the page as the next step expects it: nothing picked
  await page.evaluate(() => localStorage.clear());
  await page.reload({ waitUntil: "load" });
  await pageReady();
};

// ---- skins and effects (docs/themes.md, "Skins" and "Effects"): every theme of SKINS (lib.mjs), within the engine's limits ----
// waits until the effect layer shows `id`'s effect in `state` ("" and "": no effect at all)
const fxIs = (until, id, state) => until((i, st) => { const l = document.getElementById("fx-layer"); return (l.dataset.fx ?? "") === i && (l.dataset.state ?? "") === st; }, id, state);
// the skin <link>s on the page: [[theme id, href]]; and whether every rule of the linked skin is scoped to it (the loader
// refuses a skin that is not; this checks the sheet the browser really has)
const skinLinks = (page) => page.$$eval("link[data-look-skin]", (ls) => ls.map((l) => [l.dataset.lookSkin, l.getAttribute("href")]));
const skinScoped = (page, id) => page.evaluate((i) => {
  const sheet = [...document.styleSheets].find((s) => s.ownerNode?.dataset?.lookSkin === i);
  if (!sheet) return "no sheet";
  const scope = `:root[data-look="${i}"]`;
  const bad = [];
  const walk = (rules) => { for (const r of rules) { if (r.selectorText !== undefined) { r.selectorText.split(/,(?![^(]*\))/).forEach((sel) => { if (!sel.trim().startsWith(scope)) bad.push(sel.trim()); }); } else if (r.cssRules) walk(r.cssRules); } };
  walk(sheet.cssRules);
  return sheet.cssRules.length ? bad.join(" | ") : "empty";
}, id);
// Hit-tests the centre of every poster and every chip over a poster in view on the open tab, with the effect layer and
// everything in it made hittable on purpose (pointer-events: auto): were the layer above one of them, elementFromPoint
// would answer the layer. Returns { pe, kidsPe, hits, bad } with the layer's own pointer-events and its children's.
const posterHits = (page) => page.evaluate(() => {
  const layer = document.getElementById("fx-layer");
  const all = [layer, ...layer.querySelectorAll("*")];
  const out = { pe: getComputedStyle(layer).pointerEvents, kidsPe: [...new Set([...layer.querySelectorAll("*")].map((e) => getComputedStyle(e).pointerEvents))].join(), hits: 0, bad: [] };
  all.forEach((e) => e.style.setProperty("pointer-events", "auto", "important"));
  try {
    for (const el of document.querySelectorAll(".tab-pane.active :is(.poster, .poster-fallback, .on-image)")) {
      const b = el.getBoundingClientRect();
      if (!b.width || !b.height || getComputedStyle(el).display === "none") continue;
      const x = b.left + b.width / 2;
      const y = b.top + b.height / 2;
      if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
      out.hits++;
      const top = document.elementFromPoint(x, y);
      if (!top || layer.contains(top)) out.bad.push(`${el.className} at ${Math.round(x)},${Math.round(y)}: ${top ? top.tagName.toLowerCase() : "nothing"}`);
    }
  } finally {
    all.forEach((e) => e.style.removeProperty("pointer-events"));
  }
  return out;
});

export const steps = [
  ["the page loads: brand, header actions, a tab per panel; not a secure context", async ({ base, eq, ok, outDir, page, pageReady, panelOrder, path, tabsOf }) => {
    await page.goto(base + "/", { waitUntil: "load" });
    await pageReady();
    eq("brand", await page.$eval(".brand-name", (e) => e.textContent), "PenguWatch");
    for (const sel of ["#btn-header-add", "#btn-theme", "#head-spend", "#header-line", "#tabs"]) ok(`${sel} is on the page`, await page.$(sel) !== null);
    const tabs = await tabsOf();
    ok("the page has tabs", tabs.length >= 5);
    eq("a tab per panel, in the same order", tabs.map((t) => t.id).join(), (await panelOrder()).join());
    eq("exactly one tab is selected", await page.$$eval('.nav-btn[aria-selected="true"]', (e) => e.length), 1);
    // the run opens the app by a non-local name (lib.mjs BROWSER_HOST), as people do: no secure context, so no Clipboard API
    eq("not a secure context, no Clipboard API", await page.evaluate(() => [window.isSecureContext, "clipboard" in navigator].join()), "false,false");
    await page.screenshot({ path: path.join(outDir, "dashboard.png") });
  }, { fatal: true }],

  ["favicon, manifest and the app icons are served", async ({ eq, ok, page }) => {
    const got = await page.evaluate(async () => {
      const fetched = async (url) => { const r = await fetch(url); return `${r.status} ${r.headers.get("content-type")}`; };
      const manifest = await (await fetch("/manifest.webmanifest")).json();
      const links = [...document.querySelectorAll("link[rel*=icon]")].map((l) => l.getAttribute("href"));
      const out = { favicon: await fetched("/favicon.ico"), manifest: manifest.icons.map((i) => i.src), links, linked: {}, icons: {}, logo: null };
      for (const href of links) out.linked[href] = await fetched(href);
      for (const src of out.manifest) out.icons[src] = await fetched(src);
      const logo = document.querySelector(".logo-icon");
      out.logo = logo && logo.complete && logo.naturalWidth > 0;
      return out;
    });
    eq("favicon", got.favicon.split(" ")[0], "200");
    ok(`the manifest lists icons (${got.manifest.length})`, got.manifest.length >= 2);
    ok("the page links icons", got.links.length >= 2);
    for (const [url, status] of [...Object.entries(got.icons), ...Object.entries(got.linked)]) eq(`${url} is served`, status.split(" ")[0], "200");
    eq("the header logo is drawn", got.logo, true);
  }],

  ["desktop (1440 px): every tab opens without errors and nothing sticks out sideways", async ({ DESKTOP, eq, page, pageReady, ...ctx }) => {
    await page.setViewport(DESKTOP);
    await pageReady();
    eq("tabs", await visitTabs({ page, ...ctx }), "");
  }],

  ["desktop tab strip: at 1024 to 1920 px every tab is fully inside the strip, unscrolled; full labels only when they fit (every theme)", async ({ THEMES, eq, layoutSettled, ok, page, pageReady, pickLook, sleep }) => {
    const measure = () => page.evaluate(() => {
      const bar = document.getElementById("tabs").getBoundingClientRect();
      const out = [...document.querySelectorAll(".nav-btn")].filter((b) => b.offsetParent).map((b) => { const r = b.getBoundingClientRect(); return { id: b.id, out: r.left < bar.left - 0.5 || r.right > bar.right + 0.5 }; });
      const bad = out.filter((x) => x.out).map((x) => x.id);
      const full = document.querySelector(".nav-full");
      return { bad, shown: out.length, scrolled: document.getElementById("tabs").scrollLeft, compact: document.getElementById("tabs").classList.contains("tabs-compact"), icons: document.getElementById("tabs").classList.contains("tabs-icons"), scrollable: document.getElementById("tabs").classList.contains("tabs-scroll"), fullShown: getComputedStyle(full).display !== "none" };
    });
    for (const look of ["", ...THEMES.map(([id]) => id).filter((id) => id !== "default")]) {
      await pickLook(look);
      const seen = [];
      for (const width of [1024, 1180, 1280, 1366, 1440, 1920]) {
        await page.setViewport({ width, height: 900 });
        await pageReady();
        await layoutSettled();
        await sleep(250);
        const m = await measure();
        seen.push(`${width}:${m.icons ? "icons" : m.compact ? "short" : "full"}${m.scrollable ? "+scroll" : ""}`);
        eq(`${look || "default"} at ${width}: tabs outside the strip`, m.bad.join(), "");
        eq(`${look || "default"} at ${width}: strip scrolled`, m.scrolled, 0);
        eq(`${look || "default"} at ${width}: strip made scrollable`, m.scrollable, false);
        eq(`${look || "default"} at ${width}: all tabs shown`, m.shown, 12);
        ok(`${look || "default"} at ${width}: labels match the class`, m.fullShown === !m.compact);
        if (width === 1920) ok("the full labels show at 1920", m.fullShown && !m.compact);
      }
      console.log(`  tab labels (${look || "default"}): ${seen.join(" ")}`);
    }
    await pickLook("");
    await page.setViewport({ width: 1440, height: 1000 });
  }],

  ["phone (390 px): every tab opens from the bottom bar or More sheet, nothing sticks out sideways", async ({ click, eq, layoutSettled, ok, page, phone, problems, sleep, sticksOut, tabsOf, until }) => {
    await phone(async () => {
      ok("the More button of the bottom bar is shown", await page.$eval("#nav-more", (e) => e.getBoundingClientRect().width > 0));
      const wrong = [];
      for (const t of await tabsOf()) {
        const before = problems.length;
        const onBar = await page.$eval(`#tab-${t.id}`, (b) => "bar" in b.dataset);
        if (onBar) {
          await click(`#tab-${t.id}`);
        } else {
          await click("#nav-more");
          await until(() => !document.getElementById("more-sheet").hidden);
          await click(`#more-list [data-more-tab="${t.tab}"]`);
        }
        await until((r) => document.querySelector(`.nav-btn.active[data-route="${r}"]`) && location.hash === `#/${r}`, t.route);
        await layoutSettled();
        await sleep(300);
        const out = await sticksOut();
        if (out.length) wrong.push(`${t.id}: sticks out: ${out.join("; ")}`);
        if (problems.length > before) wrong.push(`${t.id}: ${problems.slice(before).join("; ")}`);
      }
      eq("tabs on a phone", wrong.join(" | "), "");
    });
  }],

  ["theme menu: opens from the header button with the keyboard, shows the choice, and closes on Escape, an outside click and a pick", async ({ THEMES, THEME_NAMES, click, eq, menuOpen, ok, page, pickLook, readMenu, until }) => {
    eq("closed at the start", await menuOpen(), false);
    eq("the button says it opens a menu", await page.$eval("#btn-theme", (b) => [b.getAttribute("aria-haspopup"), b.getAttribute("aria-expanded"), b.getAttribute("aria-controls")].join()), "menu,false,theme-menu");
    await page.$eval("#btn-theme", (b) => b.focus());
    await page.keyboard.press("Enter");
    await until(() => !document.getElementById("theme-menu").hidden);
    eq("expanded", await page.$eval("#btn-theme", (b) => b.getAttribute("aria-expanded")), "true");
    ok("focus moves into the menu", await page.evaluate(() => document.getElementById("theme-menu").contains(document.activeElement)));
    await page.keyboard.press("Tab");
    ok("Tab stays in the menu's items", await page.evaluate(() => document.getElementById("theme-menu").contains(document.activeElement)));
    await page.keyboard.press("Escape");
    await until(() => document.getElementById("theme-menu").hidden);
    eq("Escape: collapsed, focus back on the button", await page.evaluate(() => [document.getElementById("btn-theme").getAttribute("aria-expanded"), document.activeElement.id].join()), "false,btn-theme");
    await click("#btn-theme");
    await until(() => !document.getElementById("theme-menu").hidden);
    await click("#main");
    await until(() => document.getElementById("theme-menu").hidden);
    eq("an outside click closes it", await menuOpen(), false);
    // the choice it shows: with nothing picked, "Use the default" and "Like the system"
    const items = await readMenu();
    eq("a radio per theme, the default, and light, dark, system", items.map((i) => i.action).join(), ["pick-look", ...Array(THEMES.length).fill("pick-look"), "pick-mode", "pick-mode", "pick-mode"].join());
    eq("the checked items", items.filter((i) => i.checked).map((i) => `${i.action}:${i.arg}`).join(), "pick-look:,pick-mode:");
    ok("every theme is listed by name", Object.values(THEME_NAMES).every((n) => items.some((i) => i.text.startsWith(n))));
    eq("one-mode themes say so", items.filter((i) => / only$/.test(i.text)).map((i) => i.arg).sort().join(), THEMES.filter(([, modes]) => modes.length === 1).map(([id]) => id).sort().join());
    // a pick with the keyboard
    await click("#btn-theme");
    await until(() => !document.getElementById("theme-menu").hidden);
    await page.$eval('#theme-menu [data-arg="dracula"]', (b) => b.focus());
    await page.keyboard.press("Enter");
    await until(() => document.documentElement.dataset.look === "dracula" && document.getElementById("theme-menu").hidden);
    eq("Enter on an item picks it and closes the menu", await page.evaluate(() => [document.documentElement.dataset.look, document.documentElement.dataset.theme].join()), "dracula,dark");
    const after = await readMenu();
    eq("the menu shows the pick (and Dracula's one mode)", after.filter((i) => i.checked).map((i) => `${i.action}:${i.arg}`).join(), "pick-look:dracula,pick-mode:dark");
    ok("light and dark and system cannot be picked there", after.filter((i) => i.action === "pick-mode").every((i) => i.disabled));
    await pickLook("");
    eq("back to the default", await page.evaluate(() => [document.documentElement.dataset.look, localStorage.getItem("penguwatch.look")].join()), "default,");
  }],

  ["theme menu at 390 and 1440 px: the header still fits, the menu is inside the window and scrolls when taller than it, its items are in keyboard order under Palettes and Skins, and the Animation item is reachable", async ({ DESKTOP, PHONE, SKINS, THEMES, click, eq, ok, page, pageReady, phone, sticksOut, until }) => {
    const skins = SKINS.map(([id]) => id);
    const check = async (what) => {
      await click("#btn-theme");
      await until(() => !document.getElementById("theme-menu").hidden);
      const r = await page.$eval("#theme-menu", (m) => { const b = m.getBoundingClientRect(); return { left: Math.round(b.left), right: Math.round(b.right), top: Math.round(b.top), bottom: Math.round(b.bottom), w: innerWidth, h: innerHeight, tall: m.scrollHeight > m.clientHeight + 1, overflow: getComputedStyle(m).overflowY }; });
      ok(`${what}: the menu is inside the window (${r.left}..${r.right} of ${r.w}, ${r.top}..${r.bottom} of ${r.h})`, r.left >= 0 && r.right <= r.w && r.top >= 0 && r.bottom <= r.h);
      eq(`${what}: it scrolls`, r.overflow, "auto");
      // the groups: a heading, then its themes; the skins after "Skins"
      const order = await page.$$eval("#theme-menu .tm-h, #theme-menu .tm-sub, #theme-menu button", (els) => els.map((e) => (e.tagName === "BUTTON" ? `${e.dataset.action}:${e.dataset.arg}` : `# ${e.textContent.trim()}`)));
      const at = (x) => order.indexOf(x);
      ok(`${what}: Palettes, then Skins, then Light or dark (${order.filter((x) => x.startsWith("#")).join(", ")})`, at("# Palettes") > at("pick-look:") && at("# Skins") > at("# Palettes") && at("# Light or dark") > at("# Skins"));
      eq(`${what}: the themes under Skins`, order.slice(at("# Skins") + 1, at("# Light or dark")).filter((x) => x.startsWith("pick-look:")).map((x) => x.slice(10)).join(), skins.join());
      eq(`${what}: the themes under Palettes`, order.slice(at("# Palettes") + 1, at("# Skins")).map((x) => x.slice(10)).join(), THEMES.map(([id]) => id).filter((id) => !skins.includes(id)).join());
      // keyboard order: Tab from the first item visits every item in the order shown, the Animation item last
      const buttons = order.filter((x) => !x.startsWith("#"));
      const visited = [await page.evaluate(() => `${document.activeElement.dataset.action}:${document.activeElement.dataset.arg}`)];
      for (let i = 1; i < buttons.length; i++) {
        await page.keyboard.press("Tab");
        visited.push(await page.evaluate(() => `${document.activeElement.dataset.action}:${document.activeElement.dataset.arg}`));
      }
      eq(`${what}: Tab order`, visited.join(" "), buttons.join(" "));
      ok(`${what}: the last item is Animation`, /^toggle-effects:/.test(visited.at(-1) ?? ""));
      const anim = await page.$eval("#theme-menu [data-action=toggle-effects]", (b) => { const m = b.closest("#theme-menu").getBoundingClientRect(), r = b.getBoundingClientRect(); return { text: b.textContent.trim(), shown: r.height > 0 && r.top >= m.top - 0.5 && r.bottom <= m.bottom + 0.5 && r.bottom <= innerHeight }; });
      ok(`${what}: with the focus on it, the Animation item is in view (${anim.text})`, anim.shown && /^Animation: (on|off)$/.test(anim.text));
      if (r.tall) ok(`${what}: taller than the window allows, it scrolled to the focused item`, await page.$eval("#theme-menu", (m) => m.scrollTop > 0));
      await page.keyboard.press("Escape");
      await until(() => document.getElementById("theme-menu").hidden);
      return r.tall;
    };
    await phone(async () => {
      eq("header fits", (await sticksOut()).join("; "), "");
      ok("the theme button is shown", await page.$eval("#btn-theme", (b) => { const r = b.getBoundingClientRect(); return r.width > 0 && r.left >= 0 && r.right <= innerWidth; }));
      await check(`${PHONE.width} px`);
    });
    await check(`${DESKTOP.width}x${DESKTOP.height}`);
    // a short window: the menu must scroll
    await page.setViewport({ width: DESKTOP.width, height: 600 });
    await pageReady();
    ok("in a 600 px high window the menu is taller than it, and scrolls", await check(`${DESKTOP.width}x600`));
    await page.setViewport(DESKTOP);
    await pageReady();
  }],

  ["theme: light, dark or the system's are picked in the menu, kept in localStorage and across a reload", async ({ MODE_KEY, eq, flipMode, ok, page, pageReady, pickMode, storedMode, themeNow }) => {
    const first = await themeNow();
    const second = first === "dark" ? "light" : "dark";
    eq("nothing is stored before a pick", await storedMode(), null);
    const background = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    const firstBackground = await background();
    await flipMode();
    eq("the other mode shows", await themeNow(), second);
    eq("<html data-theme>", await page.evaluate(() => document.documentElement.dataset.theme), second);
    eq(`localStorage ${MODE_KEY}`, await storedMode(), second);
    ok("the page is really re-coloured", (await background()) !== firstBackground);
    eq("the theme-color meta follows --bg", await page.$eval('meta[name="theme-color"]', (m) => m.content.toLowerCase()), await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--bg").trim().toLowerCase()));
    await page.reload({ waitUntil: "load" });
    await pageReady();
    eq("the pick survives a reload", await themeNow(), second);
    eq("data-theme is set from storage (theme.js)", await page.evaluate(() => document.documentElement.dataset.theme), second);
    await flipMode();
    eq("and back", await themeNow(), first);
    eq(`localStorage ${MODE_KEY} follows`, await storedMode(), first);
    await page.reload({ waitUntil: "load" });
    await pageReady();
    eq("the second pick survives a reload too", await themeNow(), first);
    await pickMode("");
    eq("Like the system: the key is gone and data-theme is not set", await page.evaluate((k) => [localStorage.getItem(k), document.documentElement.dataset.theme ?? null].join(), MODE_KEY), ",");
  }],

  ["theme: with no pick the page follows the system (prefers-color-scheme)", async ({ eq, page, pageReady, themeNow }) => {
    for (const scheme of ["dark", "light"]) {
      await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
      await page.evaluate(() => localStorage.clear());
      await page.reload({ waitUntil: "load" });
      await pageReady();
      eq(`no pick, system ${scheme}`, await themeNow(), scheme);
      eq("data-theme is not set", await page.evaluate(() => document.documentElement.dataset.theme ?? null), null);
    }
  }, { chromiumOnly: "page.emulateMediaFeatures is CDP only" }],

  ["themes at 1440 px: every theme in each of its modes re-colours the page, shows in the menu and survives a reload", async ({ DESKTOP, page, pageReady, ...ctx }) => {
    await page.setViewport(DESKTOP);
    await pageReady();
    await checkLooks({ page, pageReady, ...ctx });
  }],

  ["themes at 390 px: every theme in each of its modes, nothing sticks out sideways", async ({ page, phone, ...ctx }) => {
    await phone(async () => { await checkLooks({ page, ...ctx }); });
  }],

  ["themes: the old penguwatch.theme key (light or dark, from the header button before themes) becomes the mode, once", async ({ LOOK_KEY, MODE_KEY, OLD_THEME_KEY, eq, page, pageReady, stored, themeNow }) => {
    const seed = async (old, mode) => {
      await page.evaluate(([o, m, keys]) => { localStorage.clear(); if (o) localStorage.setItem(keys.old, o); if (m) localStorage.setItem(keys.mode, m); }, [old, mode, { old: OLD_THEME_KEY, mode: MODE_KEY }]);
      await page.reload({ waitUntil: "load" });
      await pageReady();
    };
    const state = async () => (await Promise.all([OLD_THEME_KEY, MODE_KEY, LOOK_KEY].map(stored))).join();
    for (const old of ["light", "dark"]) {
      await seed(old, "");
      eq(`old "${old}": the mode shows`, await page.evaluate(() => document.documentElement.dataset.theme), old);
      eq(`old "${old}": mode kept in the new key, the theme not set, the old key gone`, await state(), `,${old},`);
      eq("the theme is the server's default", await page.evaluate(() => document.documentElement.dataset.look), await page.evaluate(() => document.documentElement.dataset.lookDefault));
      await page.reload({ waitUntil: "load" });
      await pageReady();
      eq("and a reload does not need the old key", [await themeNow(), await state()].join("|"), `${old}|,${old},`);
    }
    await seed("blue", "");
    eq("an old value that is not light or dark is dropped", [await state(), await page.evaluate(() => document.documentElement.dataset.theme ?? null)].join("|"), ",,|");
    await seed("light", "dark");
    eq("a mode already picked in the new key wins; the old key goes", [await state(), await page.evaluate(() => document.documentElement.dataset.theme)].join("|"), ",dark,|dark");
    await page.evaluate(() => localStorage.clear());
    await page.reload({ waitUntil: "load" });
    await pageReady();
  }],

  ["themes: the sign-in page follows the browser's light/dark pick (and the old key), not the theme", async ({ base, browser, eq, ok }) => {
    const context = await browser.createBrowserContext();
    try {
      const p = await context.newPage();
      await p.goto(base + "/", { waitUntil: "load" });
      eq("the sign-in page", new URL(p.url()).pathname, "/login");
      const seen = async (keys) => {
        await p.evaluate((kv) => { localStorage.clear(); for (const [k, v] of kv) localStorage.setItem(k, v); }, Object.entries(keys));
        await p.reload({ waitUntil: "load" });
        return p.evaluate(() => [document.documentElement.dataset.theme ?? "", document.documentElement.dataset.look ?? "", getComputedStyle(document.body).backgroundColor].join("|"));
      };
      eq("light picked in the app", await seen({ "penguwatch.mode": "light", "penguwatch.look": "nord" }), "light||rgb(241, 244, 247)");
      eq("dark picked in the app", await seen({ "penguwatch.mode": "dark", "penguwatch.look": "nord" }), "dark||rgb(10, 14, 19)");
      eq("the old key", await seen({ "penguwatch.theme": "light" }), "light||rgb(241, 244, 247)");
      ok("the sign-in page links no theme stylesheet", (await p.$$("link[data-look-css]")).length === 0);
    } finally {
      await context.close();
    }
  }, { variants: ["token"] }],

  // the "themed" run: the app started with config.yaml display.theme: nord (tests/test_ui_smoke.py)
  ["display.theme: a browser with no pick gets the server's default on the first paint, a pick of its own wins, and Use the default returns to it", async ({ LOOK_KEY, MODE_KEY, eq, ok, page, pageReady, pickLook, readMenu, storedLook, tokenNow }) => {
    const { identifier } = await page.evaluateOnNewDocument(() => {
      new MutationObserver((_, obs) => {
        if (!document.body) return;
        window.__atBody = [document.documentElement.dataset.look, getComputedStyle(document.documentElement).getPropertyValue("--bg").trim()].join("|");
        obs.disconnect();
      }).observe(document, { childList: true, subtree: true });
    });
    try {
      const linked = () => page.$$eval("link[data-look-css]", (ls) => ls.map((l) => l.dataset.lookCss).join());
      await page.evaluate(() => localStorage.clear());
      await page.reload({ waitUntil: "load" });
      await pageReady();
      eq("the server's default is on <html>", await page.evaluate(() => document.documentElement.dataset.lookDefault), "nord");
      eq("nothing is picked in this browser", await storedLook(), null);
      eq("Nord is shown", await page.evaluate(() => document.documentElement.dataset.look), "nord");
      const nordBg = await tokenNow("--bg");
      ok(`Nord's background (${nordBg})`, ["#242933", "#e5e9f0"].includes(nordBg));
      eq("on <html> with its colours before the body is drawn", await page.evaluate(() => window.__atBody), `nord|${nordBg}`);
      eq("only Nord's stylesheet is linked", await linked(), "nord");
      const menu = await readMenu();
      eq("the menu names the default and has it checked", menu.filter((i) => i.checked && i.action === "pick-look").map((i) => `${i.arg}|${i.text}`).join(), "|Use the default Nord");
      eq("Nord itself is not the checked item", menu.find((i) => i.arg === "nord").checked, false);
      await pickLook("default");
      eq("the browser's own pick wins over the server's default", [await page.evaluate(() => document.documentElement.dataset.look), await storedLook(), await linked()].join("|"), "default|default|");
      await page.reload({ waitUntil: "load" });
      await pageReady();
      eq("and survives a reload", [await page.evaluate(() => document.documentElement.dataset.look), await page.evaluate(() => window.__atBody)].join("|"), `default|default|${await tokenNow("--bg")}`);
      await pickLook("");
      eq("Use the default: Nord again, the key gone", [await page.evaluate(() => document.documentElement.dataset.look), await storedLook(), await linked()].join("|"), "nord||nord");
      eq(`${MODE_KEY} was never set by a theme pick`, await page.evaluate((k) => localStorage.getItem(k), MODE_KEY), null);
      eq(`${LOOK_KEY} cleared`, await storedLook(), null);
    } finally {
      await page.removeScriptToEvaluateOnNewDocument(identifier);
    }
  }, { variants: ["themed"] }],

  ["display.theme: the old light/dark key keeps the server's default theme", async ({ eq, page, pageReady, stored }) => {
    await page.evaluate(() => { localStorage.clear(); localStorage.setItem("penguwatch.theme", "light"); });
    await page.reload({ waitUntil: "load" });
    await pageReady();
    eq("Nord in light", await page.evaluate(() => [document.documentElement.dataset.look, document.documentElement.dataset.theme].join()), "nord,light");
    eq("mode kept, theme unset, old key gone", [await stored("penguwatch.look"), await stored("penguwatch.mode"), await stored("penguwatch.theme")].join("|"), "|light|");
    await page.evaluate(() => localStorage.clear());
    await page.reload({ waitUntil: "load" });
    await pageReady();
  }, { variants: ["themed"] }],

  ["skins: picking a skinned theme links its skin.css (every rule scoped) and starts its effect behind the page; picking another unlinks the skin and stops the effect", async ({ SKINS, eq, fxNow, ok, page, pickLook, sleep, until }) => {
    eq("no effect with the standard theme", (await fxNow()).fx, "");
    for (const [id, { effect }] of SKINS) {
      await pickLook(id);
      const links = await skinLinks(page);
      eq(`${id}: one skin linked`, links.map(([i]) => i).join(), id);
      ok(`${id}: the skin is ours (${links[0][1]})`, new RegExp(`^/static/v/[\\w-]+/themes/${id}/skin\\.css$`).test(links[0][1]));
      eq(`${id}: every rule of the skin the browser has is under its data-look`, await skinScoped(page, id), "");
      if (!effect) continue;
      await fxIs(until, id, "running");
      const first = await fxNow();
      await sleep(1500);
      const later = await fxNow();
      ok(`${id}: the effect draws (${first.frames} then ${later.frames} frames)`, later.frames > first.frames && later.kids > 0);
      ok(`${id}: at most 12 frames a second (${later.fps})`, later.fps > 0 && later.fps <= 12 && later.frames - first.frames <= 12 * 1.5 + 2);
      await pickLook("nord");
      await fxIs(until, "", "");
      const after = await fxNow();
      eq(`${id} -> nord: the skin is gone, the layer is empty`, [(await skinLinks(page)).length, after.kids].join(), "0,0");
      await sleep(800);
      eq(`${id} -> nord: no frame after the stop`, (await fxNow()).frames, after.frames);
    }
    await pickLook("");
  }, { variants: ["demo"] }],

  ["skins: the effect never starts while the system asks for reduced motion, starts when it stops asking, and stops when it asks again", async ({ SKINS, eq, fxNow, ok, page, pageReady, pickLook, sleep, until }) => {
    for (const [id] of SKINS.filter(([, o]) => o.effect)) {
      await pickLook(id);
      await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
      await page.reload({ waitUntil: "load" });
      await pageReady();
      await sleep(1000);
      const still = await fxNow();
      eq(`${id}, reduced motion from the start: nothing runs, nothing drawn`, [still.fx, still.state, still.frames, still.kids].join(), ",,0,0");
      eq(`${id}: the skin still applies`, (await skinLinks(page)).map(([i]) => i).join(), id);
      await page.click("#btn-theme");
      await until(() => !document.getElementById("theme-menu").hidden);
      ok("the menu says why", await page.$eval("#theme-menu", (m) => /reduced motion/.test(m.textContent)));
      await page.keyboard.press("Escape");
      await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "no-preference" }]);
      await fxIs(until, id, "running");
      await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
      await fxIs(until, "", "");
      const stopped = await fxNow();
      await sleep(800);
      eq(`${id}: stopped at once when reduced motion is asked for`, [(await fxNow()).frames, stopped.kids].join(), `${stopped.frames},0`);
      await page.emulateMediaFeatures([]);
      await pickLook("");
    }
  }, { variants: ["demo"], chromiumOnly: "page.emulateMediaFeatures is CDP only" }],

  ["skins: the effect is paused while the tab is hidden and resumes when it is shown", async ({ SKINS, eq, fxNow, ok, page, pickLook, sleep, until }) => {
    for (const [id] of SKINS.filter(([, o]) => o.effect)) {
      await pickLook(id);
      await fxIs(until, id, "running");
      await sleep(600);
      await page.evaluate(() => {
        Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await fxIs(until, id, "paused");
      const paused = await fxNow();
      await sleep(1500);
      eq(`${id}: no frame while hidden`, (await fxNow()).frames, paused.frames);
      await page.evaluate(() => { delete document.hidden; document.dispatchEvent(new Event("visibilitychange")); });
      await fxIs(until, id, "running");
      await sleep(1500);
      ok(`${id}: drawing again once shown`, (await fxNow()).frames > paused.frames);
      await pickLook("");
    }
  }, { variants: ["demo"] }],

  ["skins: an effect whose frames run long is slowed down, then stopped (the frame budget)", async ({ SKINS, eq, fxNow, ok, page, pageReady, pickLook, until }) => {
    for (const [id] of SKINS.filter(([, o]) => o.effect)) {
      await pickLook(id);
      await fxIs(until, id, "running");
      const start = (await fxNow()).fps;
      // every clearRect now takes 9 ms: an effect clears its canvas each frame, so each frame is over the 4 ms budget
      await page.evaluate(() => {
        const clear = CanvasRenderingContext2D.prototype.clearRect;
        CanvasRenderingContext2D.prototype.clearRect = function (...a) { const t = performance.now() + 9; while (performance.now() < t); return clear.apply(this, a); };
      });
      await page.waitForFunction((f) => Number(document.getElementById("fx-layer").dataset.fps) < f, { timeout: 15000 }, start);
      await page.waitForFunction(() => document.getElementById("fx-layer").dataset.state === "over-budget", { timeout: 30000 });
      const stopped = await fxNow();
      eq(`${id}: given up, the layer emptied`, [stopped.fx, stopped.kids].join(), ",0");
      await page.evaluate(() => new Promise((r) => setTimeout(r, 1200)));
      eq(`${id}: no frame after giving up`, (await fxNow()).frames, stopped.frames);
      await page.reload({ waitUntil: "load" });
      await pageReady();
      await fxIs(until, id, "running");
      ok(`${id}: a new page starts it again at its own rate`, (await fxNow()).fps === start);
      await pickLook("");
    }
  }, { variants: ["demo"] }],

  ["skins: the menu's Animation item turns the effect off and on, kept in this browser and across a reload", async ({ FX_KEY, SKINS, eq, fxNow, page, pageReady, pickInMenu, pickLook, stored, until }) => {
    const animation = () => page.evaluate(() => { const b = document.querySelector("#theme-menu [data-action=toggle-effects]"); return b ? `${b.getAttribute("role")}|${b.getAttribute("aria-checked")}|${b.textContent.trim()}|${b.nextElementSibling?.textContent ?? ""}` : ""; });
    const openRead = async () => { await page.click("#btn-theme"); await until(() => !document.getElementById("theme-menu").hidden); const got = await animation(); await page.keyboard.press("Escape"); await until(() => document.getElementById("theme-menu").hidden); return got; };
    eq("with the standard theme the item is there, on, and says the theme has none", await openRead(), "menuitemcheckbox|true|Animation: on|PenguWatch has no animation.");
    for (const [id] of SKINS.filter(([, o]) => o.effect)) {
      await pickLook(id);
      await fxIs(until, id, "running");
      await pickInMenu("toggle-effects", "off");
      await fxIs(until, "", "");
      eq(`${id}: off, kept`, [await stored(FX_KEY), (await fxNow()).kids].join(), "off,0");
      eq("the menu shows it off", await openRead(), "menuitemcheckbox|false|Animation: off|");
      await page.reload({ waitUntil: "load" });
      await pageReady();
      await page.evaluate(() => new Promise((r) => setTimeout(r, 800)));
      eq(`${id}: still off after a reload`, (await fxNow()).fx, "");
      await pickInMenu("toggle-effects", "on");
      await fxIs(until, id, "running");
      eq(`${id}: on again, kept`, await stored(FX_KEY), "on");
    }
    await page.evaluate((k) => localStorage.removeItem(k), FX_KEY);
    await pickLook("");
  }, { variants: ["demo"] }],

  ["skins: no part of an effect is ever above a poster or a chip over one (hit-tested at their centres, layer made hittable), and the layer is never hit (pointer-events: none), at 1440 and 390 px", async ({ SKINS, eq, fxNow, goTab, ok, page, phone, pickLook, sleep, until, waitSome }) => {
    const check = async (id, width) => {
      for (const route of ["library", "upnext"]) {
        await goTab(route);
        await waitSome(".tab-pane.active :is(.poster, .poster-fallback)");
        await sleep(400);
        const r = await posterHits(page);
        eq(`${id} at ${width}, ${route}: the layer is never hit`, r.pe, "none");
        ok(`${id} at ${width}, ${route}: nor is anything in it (${r.kidsPe})`, r.kidsPe === "" || r.kidsPe === "none");
        ok(`${id} at ${width}, ${route}: posters hit-tested (${r.hits})`, r.hits > 0);
        eq(`${id} at ${width}, ${route}: the effect above a poster or chip`, r.bad.join("; "), "");
      }
    };
    for (const [id, { effect }] of SKINS) {
      await pickLook(id);
      if (effect) await fxIs(until, id, "running");
      ok(`${id}: the effect draws in its layer`, !effect || (await fxNow()).kids > 0);
      await check(id, 1440);
      await phone(async () => {
        if (effect) await fxIs(until, id, "running");
        await check(id, 390);
      });
    }
    await pickLook("");
  }, { variants: ["demo"] }],

  ["skins: every tab at 1440 and 390 px with each skin opens without errors and nothing sticks out sideways", async ({ DESKTOP, SKINS, click, eq, layoutSettled, page, pageReady, phone, pickLook, problems, sleep, sticksOut, tabsOf, until, ...ctx }) => {
    for (const [id] of SKINS) {
      await page.setViewport(DESKTOP);
      await pageReady();
      await pickLook(id);
      eq(`${id} at 1440: tabs`, await visitTabs({ page, problems, sleep, sticksOut, tabsOf, ...ctx }), "");
      await phone(async () => {
        const wrong = [];
        for (const t of await tabsOf()) {
          const before = problems.length;
          if (await page.$eval(`#tab-${t.id}`, (b) => "bar" in b.dataset)) await click(`#tab-${t.id}`);
          else {
            await click("#nav-more");
            await until(() => !document.getElementById("more-sheet").hidden);
            await click(`#more-list [data-more-tab="${t.tab}"]`);
          }
          await until((r) => document.querySelector(`.nav-btn.active[data-route="${r}"]`), t.route);
          await layoutSettled();
          await sleep(250);
          const out = await sticksOut();
          if (out.length) wrong.push(`${t.id}: sticks out: ${out.join("; ")}`);
          if (problems.length > before) wrong.push(`${t.id}: ${problems.slice(before).join("; ")}`);
        }
        eq(`${id} at 390: tabs`, wrong.join(" | "), "");
      });
    }
    await pickLook("");
  }, { variants: ["demo"] }],

  // the "themed" run: config.yaml display.effects: false (tests/test_ui_smoke.py)
  ["display.effects off: a browser with no choice of its own runs no effect, and its own choice in the menu wins", async ({ FX_KEY, SKINS, eq, fxNow, page, pageReady, pickInMenu, pickLook, sleep, stored, until }) => {
    eq("the server's choice is on <html>", await page.evaluate(() => document.documentElement.dataset.effectsDefault), "off");
    for (const [id] of SKINS.filter(([, o]) => o.effect)) {
      await page.evaluate((k) => localStorage.removeItem(k), FX_KEY); // the last skin's loop ended with its own "off"
      await pickLook(id);
      await sleep(1000);
      eq(`${id}: no effect, nothing stored`, [(await fxNow()).fx, await stored(FX_KEY)].join(), ",");
      await pickInMenu("toggle-effects", "on");
      await fxIs(until, id, "running");
      await page.reload({ waitUntil: "load" });
      await pageReady();
      await fxIs(until, id, "running");
      await pickInMenu("toggle-effects", "off");
      await fxIs(until, "", "");
    }
    await page.evaluate(() => localStorage.clear());
    await page.reload({ waitUntil: "load" });
    await pageReady();
  }, { variants: ["themed"] }],

  // `solo` variant (test_ui_smoke.py): the demo library with one person (scripts/demo_library.py --people 1)
  ["one person: every who control is hidden (Library, Up Next, Tonight's Pick, Add title, details, plan)", async ({ click, eq, goTab, ok, page, until, waitSome }) => {
    eq("the page knows one person", (await page.$eval('meta[name="penguwatch-household"]', (m) => JSON.parse(m.content))).map((p) => p.name).join(), "Me");
    await goTab("library");
    await waitSome(".show-card");
    eq("no Audience filter", await page.$("#filter-household-select"), null);
    eq("no who chip or who button on the cards", await page.$$eval('.show-card .who-chip, .show-card [data-field="household"]', (e) => e.length), 0);
    await goTab("upnext");
    await waitSome("#upnext-container .upnext-card");
    eq("Up Next: no person or shared chip", await page.$$eval("#upnext-chips .upnext-chip", (els) => els.map((e) => e.dataset.chip).join()), "all,active,today");
    ok("no name on the cards", await page.$$eval("#upnext-container .upnext-meta", (els) => els.every((e) => !/ · Me$/.test(e.textContent.trim()))));
    await page.evaluate(() => document.querySelector('#upnext-container [data-call="openShow"]').click());
    await until(() => document.querySelector("#episodes-modal.active #show-detail .detail-controls"));
    eq("details: no who toggles", await page.$(".detail-who"), null);
    await page.keyboard.press("Escape");
    await until(() => !document.getElementById("episodes-modal").classList.contains("active"));
    await goTab("pick");
    ok("Tonight's Pick: the Audience field is hidden", await page.$eval("#decide-audience", (s) => s.closest(".field").hidden && s.offsetParent === null));
    await click("#btn-header-add");
    await until(() => document.getElementById("add-show-modal").classList.contains("active"));
    ok("Add title: who wants to watch is hidden", await page.$eval("#m-household", (g) => g.closest(".field").hidden && g.offsetParent === null));
    await click("#btn-close-add-modal");
    await until(() => !document.getElementById("add-show-modal").classList.contains("active"));
    await goTab("optimizer");
    await waitSome("#optimizer-tab .service-metrics .stat");
    ok("the plan card has no shared stat", await page.$$eval(".stat .lbl", (els) => els.every((e) => !/^(Both want|Shared)$/.test(e.textContent.trim()))));
  }, { variants: ["solo"] }],
];
