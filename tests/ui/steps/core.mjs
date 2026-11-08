// The page as a whole: header and tabs, every tab at desktop and phone width, the theme menu and every theme, the icons.
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

  ["desktop tab strip: at 1024 to 1920 px every tab is fully inside the strip, unscrolled; full labels only when they fit (default and high-contrast themes)", async ({ eq, layoutSettled, ok, page, pageReady, pickLook, sleep }) => {
    const measure = () => page.evaluate(() => {
      const bar = document.getElementById("tabs").getBoundingClientRect();
      const out = [...document.querySelectorAll(".nav-btn")].filter((b) => b.offsetParent).map((b) => { const r = b.getBoundingClientRect(); return { id: b.id, out: r.left < bar.left - 0.5 || r.right > bar.right + 0.5 }; });
      const bad = out.filter((x) => x.out).map((x) => x.id);
      const full = document.querySelector(".nav-full");
      return { bad, shown: out.length, scrolled: document.getElementById("tabs").scrollLeft, compact: document.getElementById("tabs").classList.contains("tabs-compact"), icons: document.getElementById("tabs").classList.contains("tabs-icons"), scrollable: document.getElementById("tabs").classList.contains("tabs-scroll"), fullShown: getComputedStyle(full).display !== "none" };
    });
    for (const look of ["", "high-contrast"]) {
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

  ["theme menu: opens from the header button with the keyboard, shows the choice, and closes on Escape, an outside click and a pick", async ({ click, eq, menuOpen, ok, page, pickLook, readMenu, until }) => {
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
    eq("a radio per theme, the default, and light, dark, system", items.map((i) => i.action).join(), ["pick-look", ...Array(7).fill("pick-look"), "pick-mode", "pick-mode", "pick-mode"].join());
    eq("the checked items", items.filter((i) => i.checked).map((i) => `${i.action}:${i.arg}`).join(), "pick-look:,pick-mode:");
    ok("every theme is listed by name", ["PenguWatch", "Nord", "Dracula", "Solarized", "E-ink", "High contrast", "Blueprint"].every((n) => items.some((i) => i.text.startsWith(n))));
    ok("one-mode themes say so", items.filter((i) => / only$/.test(i.text)).map((i) => i.arg).sort().join() === "blueprint,dracula,e-ink");
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

  ["theme menu on a phone (390 px): the header still fits and the menu is inside the window", async ({ click, eq, ok, page, phone, sticksOut, until }) => {
    await phone(async () => {
      eq("header fits", (await sticksOut()).join("; "), "");
      ok("the theme button is shown", await page.$eval("#btn-theme", (b) => { const r = b.getBoundingClientRect(); return r.width > 0 && r.left >= 0 && r.right <= innerWidth; }));
      await click("#btn-theme");
      await until(() => !document.getElementById("theme-menu").hidden);
      const r = await page.$eval("#theme-menu", (m) => { const b = m.getBoundingClientRect(); return { left: Math.round(b.left), right: Math.round(b.right), top: Math.round(b.top), bottom: Math.round(b.bottom), w: innerWidth, h: innerHeight }; });
      ok(`the menu is inside the window (${r.left}..${r.right} of ${r.w}, ${r.top}..${r.bottom} of ${r.h})`, r.left >= 0 && r.right <= r.w && r.top >= 0 && r.bottom <= r.h);
      ok("and every item can be reached by scrolling it", await page.$eval("#theme-menu", (m) => getComputedStyle(m).overflowY === "auto"));
      await page.keyboard.press("Escape");
      await until(() => document.getElementById("theme-menu").hidden);
    });
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
];
