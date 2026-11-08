// Settings: the tab that edits every setting (src/penguwatch/web/static/panels/settings/).
//
// The steps talk to the real Settings API of the app under test (the demo library; its config.yaml sets only the
// database path, so the one-time import stores nothing and the store starts at revision 1). The demo steps run in order
// and each save adds one revision, counted from the revision the first save step reads (`rev`). The `pinned` variant
// (test_ui_smoke.py) adds variables: PENGUWATCH_TZ and PENGUWATCH_NOTIFY_URL pin their settings, PENGUWATCH_LOG_LEVEL has
// an invalid value (ignored, said on its row), and its config.yaml has a webhook that signs with an imported variable.
export const subject = "panel:settings";
export const variants = ["demo"];

const MAX = "optimizer.max_active_services";
const MAX_INPUT = "#set-in-optimizer-max-active-services";
const SECRET = "https://secret.example/hook-token-123";

let rev = 0; // the revision before the first save of these steps

// The PUT requests the page sends, as sent (observed, not intercepted).
const seen = { lastPut: null, puts: 0, watching: false };
const watchPuts = (page) => {
  if (seen.watching) return;
  seen.watching = true;
  page.on("request", (req) => {
    const url = new URL(req.url());
    if (req.method() !== "PUT" || url.pathname !== "/api/settings") return;
    seen.puts += 1;
    seen.lastPut = { ...JSON.parse(req.postData() || "{}"), source: url.searchParams.get("source") };
  });
};

const apiState = (page) => page.evaluate(async () => (await fetch("/api/settings")).json());
const apiVersions = (page) => page.evaluate(async () => (await fetch("/api/settings/versions")).json());
const metaShows = (page, n) => page.waitForFunction((r) => new RegExp(`revision ${r}\\b`).test(document.getElementById("settings-meta").textContent), {}, n);

// Types into a box the way a person does.
const settingsType = async (page, sel, text) => {
  await page.$eval(sel, (e) => { e.focus(); e.select(); });
  await page.keyboard.press("Backspace");
  if (text) await page.keyboard.type(text);
};

const rowText = (page, key, part) => page.$eval(`.set-row[data-key="${key}"] ${part}`, (e) => e.textContent.trim());
const saveLabel = (page) => page.$eval("#settings-save", (b) => `${b.textContent.trim()}|${b.disabled}`);

export const steps = [
  ["every section of the schema renders, every setting shows its value, default and effect", async ({ eq, goTab, ok, page, text, waitSome }) => {
    await goTab("settings");
    await waitSome(".set-sec", 8);
    const schema = await page.evaluate(async () => (await fetch("/api/settings/schema")).json());
    const ids = await page.$$eval(".set-sec", (els) => els.map((e) => e.dataset.section));
    eq("sections: the schema's, then Kept elsewhere and About", ids.join(), [...schema.sections.sort((a, b) => a.order - b.order).map((s) => s.id), "elsewhere", "about"].join());
    eq("a row per setting", await page.$$eval(".set-row[data-key]", (els) => els.length), schema.settings.length);
    const wrong = await page.evaluate((specs) => specs.filter((s) => {
      const row = document.querySelector(`.set-row[data-key="${s.key}"]`);
      return !row || row.querySelector(".set-effect")?.textContent !== s.effect || !/^Default: /.test(row.querySelector(".set-def")?.textContent ?? "")
        || !row.querySelector(".set-label")?.textContent.trim();
    }).map((s) => s.key), schema.settings);
    eq("rows without their label, effect or default", wrong.join(), "");
    // the demo's config.yaml does not set the optimizer, so the import stored the value it ran with (import rule 2)
    const running = (await apiState(page)).values[MAX].value;
    eq("the current value of a number is the server's", await page.$eval(MAX_INPUT, (i) => i.value), String(running));
    eq("its default line", await rowText(page, MAX, ".set-def"), "Default: 3");
    const restart = schema.settings.filter((s) => !s.live).length;
    eq("one 'Applies after a restart' chip per driver-role setting", await page.$$eval("[data-badge=restart-role]", (c) => c.length), restart);
    ok("the restart help names the container command", /docker compose restart/.test(await text("#set-sec-sources")));
    ok("Advanced is a disclosure", await page.$eval("#set-sec-advanced", (d) => d.tagName === "DETAILS"));
    ok("it warns that there is no access token", /No access token set: anyone who can reach this port can change these settings, including where notifications are sent/.test(await text("#settings-banner")));
    eq("links to the tabs that edit the rest", await page.$$eval("#set-sec-elsewhere a", (a) => a.map((x) => x.getAttribute("href")).join()), "#/services,#/ratings,#/services");
    ok("About lists the install", /Version\s*\d+\.\d+\.\d+/.test((await text("#set-sec-about")).replace(/\s+/g, " ")));
  }],

  ["every control has a name and every row is reachable by keyboard", async ({ eq, ok, page }) => {
    const nameless = await page.evaluate(() => [...document.querySelectorAll("#settings-tab input, #settings-tab select, #settings-tab button, #settings-tab textarea")]
      .filter((e) => e.type !== "hidden" && !e.closest("[hidden]") && !(e.getAttribute("aria-label") || e.getAttribute("aria-labelledby") || e.labels?.[0]?.textContent.trim() || e.textContent.trim() || e.title))
      .map((e) => `${e.tagName}#${e.id}`));
    eq("controls without a name", nameless.join(), "");
    await page.$eval(MAX_INPUT, (i) => i.focus());
    await page.keyboard.press("Tab");
    ok("Tab moves on to the next control", await page.evaluate(() => document.activeElement.closest("#settings-tab") !== null && document.activeElement.id !== "set-in-optimizer-max-active-services"));
    ok("the Versions button can be focused", await page.$eval('#settings-bar [data-action="settings-versions"]', (b) => { b.focus(); return document.activeElement === b; }));
    eq("Save and Cancel are off while there is no change", await page.$eval("#settings-bar", (b) => [...b.querySelectorAll("#settings-save, #settings-cancel")].map((x) => x.disabled).join()), "true,true");
  }],

  ["edit a number: an error shows and blocks Save, a valid value is saved and persists after a reload", async (ctx) => {
    const { eq, expectStatus, goTab, ok, page, resetPage, until, waitSome } = ctx;
    watchPuts(page);
    expectStatus(409, 422);
    await resetPage();
    await goTab("settings");
    await waitSome(".set-sec", 8);
    rev = (await apiState(page)).revision;
    ok(`the store has a revision (${rev})`, rev >= 1);
    await settingsType(page, MAX_INPUT, "99");
    await until((k) => /1 to 10/.test(document.querySelector(`.set-row[data-key="${k}"] [data-msg]`)?.textContent ?? ""), MAX);
    eq("Save is off while the value is refused", (await saveLabel(page)).endsWith("true"), true);
    await settingsType(page, MAX_INPUT, "5");
    await until((k) => document.querySelector(`.set-row[data-key="${k}"] [data-msg]`)?.textContent === "", MAX);
    eq("Save counts the change", await saveLabel(page), "Save 1 change|false");
    ok("the row is marked Unsaved", /Unsaved/.test(await rowText(page, MAX, "[data-meta]")));
    // a refusal only the server makes: the page offers the hours 0-23, so a 24 is put into the box by hand
    await page.$eval("#set-in-notify-hour", (s) => { const o = document.createElement("option"); o.value = "24"; o.textContent = "24:00"; o.dataset.injected = ""; s.append(o); });
    await page.select("#set-in-notify-hour", "24");
    await until(() => /at most 23/.test(document.querySelector('.set-row[data-key="notify.hour"] [data-msg]')?.textContent ?? ""));
    eq("Save is off while the server refuses a value", (await saveLabel(page)).endsWith("true"), true);
    await page.select("#set-in-notify-hour", "9");
    await page.$eval("#set-in-notify-hour option[data-injected]", (o) => o.remove());
    await until(() => document.querySelector('.set-row[data-key="notify.hour"] [data-msg]')?.textContent === "");
    await until(() => !document.getElementById("settings-save").disabled);
    await ctx.click("#settings-save");
    await metaShows(page, rev + 1);
    eq("the request carried the draft and the revision it loaded", JSON.stringify([seen.lastPut.base_revision, seen.lastPut.values]), JSON.stringify([rev, { [MAX]: 5 }]));
    ok("the toast says what was saved", /Services kept at once/.test(await page.$eval("#toast", (t) => t.textContent)));
    ok("the row shows Changed", /Changed/.test(await rowText(page, MAX, "[data-meta]")));
    await resetPage();
    await goTab("settings");
    await waitSome(".set-sec", 8);
    eq("the value is still 5 after a reload", await page.$eval(MAX_INPUT, (i) => i.value), "5");
    eq("the server has it", (await apiState(page)).values[MAX].stored, 5);
  }],

  ["Reset to default restores the default value and saves a null", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    await ctx.click(`.set-row[data-key="${MAX}"] [data-action=settings-reset]`);
    eq("the box shows the default", await page.$eval(MAX_INPUT, (i) => i.value), "3");
    ok("nothing shows Reset any more", await page.$(`.set-row[data-key="${MAX}"] [data-action=settings-reset]`) === null);
    await until(() => !document.getElementById("settings-save").disabled);
    await ctx.click("#settings-save");
    await metaShows(page, rev + 2);
    eq("the request reset the key", JSON.stringify(seen.lastPut.values), JSON.stringify({ [MAX]: null }));
    ok("no Changed chip", !/Changed/.test(await rowText(page, MAX, "[data-meta]")));
    eq("the server has no stored value", (await apiState(page)).values[MAX].stored, null);
  }],

  ["a change made elsewhere (409) keeps the other edits and says what was kept", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    await settingsType(page, MAX_INPUT, "4");
    await page.select("#set-in-notify-hour", "7");
    await until(() => !document.getElementById("settings-save").disabled);
    // a second writer (another browser, the API) saves the same setting first
    const other = await page.evaluate(async (key) => {
      const state = await (await fetch("/api/settings")).json();
      const res = await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_revision: state.revision, values: { [key]: 2 }, lists: {}, secrets: {} }) });
      return [res.status, (await res.json()).revision];
    }, MAX);
    eq("the other write was saved", JSON.stringify(other), JSON.stringify([200, rev + 3]));
    await ctx.click("#settings-save");
    await until(() => document.querySelector("#settings-notes [data-note]"));
    ok("the note names the setting that changed elsewhere", /Changed elsewhere[^]*Services kept at once/.test(await ctx.text("#settings-notes")));
    eq("the other edit is still in the draft", `${await saveLabel(page)} ${await page.$$eval(".set-row.unsaved", (r) => r.map((x) => x.dataset.key).join())}`, "Save 1 change|false notify.hour");
    ok("the note names the newer revision", new RegExp(`now at revision ${rev + 3}\\b`).test(await ctx.text("#settings-notes")));
    eq("the page took the other writer's value", await page.$eval(MAX_INPUT, (i) => i.value), "2");
    await ctx.click("#settings-cancel");
    await until(() => document.getElementById("settings-confirm-modal").classList.contains("active"));
    await ctx.click("#settings-confirm-yes");
    await until(() => document.getElementById("settings-save").disabled);
  }],

  ["a secret is write-only: typed in a password box, saved, never shown again", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    await ctx.click('[data-slot="notify.url"] [data-action=settings-secret-replace]');
    eq("the box is a password box", await page.$eval('[data-slot="notify.url"] input', (i) => i.type), "password");
    await page.type('[data-slot="notify.url"] input', SECRET);
    eq("the row says a new value was entered, not which", (await ctx.text('[data-slot="notify.url"] [data-state]')).trim(), "New value entered (unsaved)");
    ok("the text is nowhere in the page's markup", !(await page.content()).includes("hook-token-123"));
    await until(() => !document.getElementById("settings-save").disabled);
    await ctx.click("#settings-save");
    await until(() => /Set · secret\.example/.test(document.querySelector('[data-slot="notify.url"] [data-state]')?.textContent ?? ""));
    await metaShows(page, rev + 4);
    eq("the request carried the secret", seen.lastPut.secrets["notify.url"], SECRET);
    ok("afterwards only the host shows", !(await page.content()).includes("hook-token-123"));
    eq("no box is open and none holds a value", await page.$$eval('[data-slot="notify.url"] input', (i) => i.map((x) => x.value).join("")), "");
    ok("Replace and Remove are offered", await page.$('[data-slot="notify.url"] [data-action=settings-secret-remove]') !== null);
    const state = await apiState(page);
    eq("the server says set, stored, and the host only", JSON.stringify(state.secrets["notify.url"]), JSON.stringify({ set: true, source: "stored", host: "secret.example" }));
  }],

  ["versions list and restore as a draft, saved as a restore; export shows YAML; an import is a draft, saved as one", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    const puts = seen.puts;
    await ctx.click('#settings-bar [data-action="settings-versions"]');
    await until((n) => document.querySelector(`#settings-versions-body [data-action="settings-restore"][data-n="${n}"]`), rev + 1);
    eq("one version per save", await page.$$eval("#settings-versions-body .set-vers li", (l) => l.length), rev + 4);
    await ctx.click(`#settings-versions-body [data-action="settings-restore"][data-n="${rev + 1}"]`);
    await until(() => !document.getElementById("settings-versions-modal").classList.contains("active"));
    ok("the note says the version is a draft", /is on the page as a draft/.test(await ctx.text("#settings-notes")));
    eq("restoring only changed the draft: nothing was sent", seen.puts, puts);
    eq("the draft holds the restored value", await page.$eval(MAX_INPUT, (i) => i.value), "5");
    await until(() => !document.getElementById("settings-save").disabled);
    await ctx.click("#settings-save");
    await metaShows(page, rev + 5);
    eq("Save told the server it is a restore", seen.lastPut.source, "restore");
    eq("the versions say so", (await apiVersions(page))[0].source, "restore");
    await ctx.click('#settings-bar [data-action="settings-export"]');
    await until(() => /max_active_services: 5/.test(document.getElementById("settings-export-text").value));
    // the header says in words that no secret is in the file; what must not be there is the address saved above, or any URL
    ok("the export has no address or secret", !/hook-token-123|secret\.example|https?:\/\//.test(await page.$eval("#settings-export-text", (t) => t.value)));
    await page.keyboard.press("Escape");
    await ctx.click('#settings-bar [data-action="settings-import"]');
    const yaml = "optimizer:\n  max_active_services: 6\nservices: []\n";
    await page.type("#settings-import-text", yaml);
    await ctx.click('[data-action="settings-import-read"]');
    await until(() => !document.getElementById("settings-import-modal").classList.contains("active"));
    eq("the import is a draft, not saved", `${await page.$eval(MAX_INPUT, (i) => i.value)} ${seen.puts}`, `6 ${puts + 1}`);
    ok("the notes say what was left out", /edited on the Services/.test(await ctx.text("#settings-notes")));
    await ctx.click("#settings-save");
    await metaShows(page, rev + 6);
    eq("Save told the server the source", seen.lastPut.source, "yaml_import");
    eq("the versions say so, newest first", (await apiVersions(page)).slice(0, 2).map((v) => v.source).join(), "yaml_import,restore");
  }],

  ["a new webhook needs its address before Save; the address and secret go to the secrets map, never into the list", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    await ctx.click('[data-action="settings-webhook-add"]');
    await until(() => document.querySelector(".set-card[open]"));
    eq("Save is off: the new webhook has no address", (await saveLabel(page)).endsWith("true"), true);
    ok("the card says why", /Address needed/.test(await ctx.text(".set-card summary")));
    eq("a new webhook can't name a variable or a file for its secret", await page.$$eval(".set-card [data-wf=secret_env], .set-card [data-wf=secret_file], .set-card [data-action=settings-webhook-stop-import]", (e) => e.length), 0);
    await settingsType(page, ".set-card [data-wf=name]", "Home Assistant");
    await page.$eval(".set-card [data-wf=name]", (e) => e.dispatchEvent(new Event("change", { bubbles: true })));
    await until(() => document.querySelector('.set-card[data-id="home-assistant"]'));
    await ctx.click('.set-card [data-slot="webhook.home-assistant.url"] [data-action=settings-secret-replace]');
    await page.type('.set-card [data-slot="webhook.home-assistant.url"] input', "https://192.0.2.10/hook-abc");
    await ctx.click('.set-card [data-slot="webhook.home-assistant.secret"] [data-action=settings-secret-replace]');
    await page.type('.set-card [data-slot="webhook.home-assistant.secret"] input', "0123456789abcdef");
    await until(() => !document.getElementById("settings-save").disabled);
    await ctx.click("#settings-save");
    await metaShows(page, rev + 7);
    const put = seen.lastPut;
    eq("the list item has an id and a name but no address or secret", JSON.stringify([put.lists.webhooks[0].id, put.lists.webhooks[0].name, "url" in put.lists.webhooks[0], "secret" in put.lists.webhooks[0]]), JSON.stringify(["home-assistant", "Home Assistant", false, false]));
    eq("the slots carry them", JSON.stringify(Object.keys(put.secrets).sort()), JSON.stringify(["webhook.home-assistant.secret", "webhook.home-assistant.url"]));
    ok("nothing of either is in the page afterwards", !/hook-abc|0123456789abcdef/.test(await page.content()));
    const state = await apiState(page);
    eq("the server keeps the hook without its address", JSON.stringify(state.lists.webhooks.items.map((i) => [i.id, "url" in i])), JSON.stringify([["home-assistant", false]]));
    eq("its address and secret are stored", JSON.stringify([state.secrets["webhook.home-assistant.url"], state.secrets["webhook.home-assistant.secret"].source]), JSON.stringify([{ set: true, source: "stored", host: "192.0.2.10" }, "stored"]));
    ok("nothing of either is in the answer", !/hook-abc|0123456789abcdef/.test(JSON.stringify(state)));
  }],

  ["leaving the tab with a draft asks first; Keep editing stays", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    await settingsType(page, MAX_INPUT, "8");
    await page.evaluate(() => { location.hash = "#/system"; });
    await until(() => document.getElementById("settings-confirm-modal").classList.contains("active"));
    eq("the Settings tab is still the one shown", await page.$eval(".tab-pane.active", (p) => p.id), "settings-tab");
    ok("the question says why", /unsaved change/.test(await ctx.text("#settings-confirm-text")));
    await ctx.click("#settings-confirm-no");
    eq("the draft is kept", await page.$eval(MAX_INPUT, (i) => i.value), "8");
    await page.evaluate(() => { location.hash = "#/system"; });
    await until(() => document.getElementById("settings-confirm-modal").classList.contains("active"));
    await ctx.click("#settings-confirm-yes");
    await until(() => document.querySelector(".tab-pane.active")?.id === "system-tab");
    await ctx.goTab("settings");
    eq("the discarded draft is gone", await page.$eval(MAX_INPUT, (i) => i.value), "6");
  }],

  ["390 px: one column, a jump list instead of the rail, the bar fits, nothing sticks out", async ({ eq, goTab, ok, page, phone, sticksOut, waitSome }) => {
    await phone(async () => {
      await goTab("settings");
      await waitSome(".set-sec", 8);
      eq("nothing sticks out sideways", (await sticksOut()).join("; "), "");
      ok("the jump list is shown and the rail is not", await page.evaluate(() => getComputedStyle(document.getElementById("settings-jump")).display !== "none" && getComputedStyle(document.getElementById("settings-rail")).display === "none"));
      ok("the sticky bar is inside the window", await page.$eval("#settings-bar", (b) => { const r = b.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; }));
      await page.select("#settings-jump", "display");
      await page.waitForFunction(() => document.getElementById("set-sec-display").getBoundingClientRect().top < innerHeight);
      eq("nothing sticks out after jumping", (await sticksOut()).join("; "), "");
    });
  }],

  ["390 px: no bar while nothing changed (Versions, Export, Import stay in a row on top); one edit shows a slim bar that does not cover the field; Save removes it", async (ctx) => {
    const { eq, goTab, ok, page, phone, sticksOut, until, waitSome } = ctx;
    const barShown = () => page.$eval("#settings-bar", (b) => getComputedStyle(b).display !== "none");
    await phone(async () => { try {
      await goTab("settings");
      await waitSome(".set-sec", 8);
      ok("no sticky bar while nothing has changed", !(await barShown()));
      for (const [action, name] of [["settings-versions", "Versions"], ["settings-export", "Export"], ["settings-import", "Import"]]) {
        const sel = `.set-tools [data-action="${action}"]`;
        const got = await page.$eval(sel, (b) => { const r = b.getBoundingClientRect(); b.focus(); return { name: b.textContent.trim(), focused: document.activeElement === b, inside: r.width > 0 && r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.top < innerHeight }; });
        eq(`${name}: visible, named and focusable in the top row`, `${got.name}|${got.focused}|${got.inside}`, `${name}|true|true`);
      }
      await ctx.click('.set-tools [data-action="settings-versions"]');
      await until(() => document.getElementById("settings-versions-modal").classList.contains("active"));
      await page.keyboard.press("Escape");
      await until(() => !document.getElementById("settings-versions-modal").classList.contains("active"));
      // put the field just above the tab bar (where the bar will appear), then edit it
      await page.$eval(MAX_INPUT, (i) => { i.scrollIntoView({ block: "center" }); window.scrollBy(0, i.getBoundingClientRect().bottom - (innerHeight - 70)); });
      await settingsType(page, MAX_INPUT, "7");
      await until(() => document.getElementById("settings-bar").classList.contains("dirty"));
      eq("the bar counts the change", await ctx.text("#settings-count"), "1 change");
      ok("the bar has no Versions, Export or Import", await page.$eval("#settings-bar", (b) => [...b.querySelectorAll('[data-action="settings-versions"], [data-action="settings-export"], [data-action="settings-import"]')].every((x) => x.offsetParent === null)));
      await ctx.layoutSettled();
      const boxes = await page.evaluate((sel) => {
        const i = document.querySelector(sel).getBoundingClientRect(), b = document.getElementById("settings-bar").getBoundingClientRect(), t = document.querySelector(".tabs")?.getBoundingClientRect();
        return { inputBottom: Math.round(i.bottom), barTop: Math.round(b.top), barBottom: Math.round(b.bottom), barH: Math.round(b.height), inputTop: Math.round(i.top), inside: b.left >= 0 && b.right <= innerWidth, tabTop: t ? Math.round(t.top) : null };
      }, MAX_INPUT);
      ok(`the focused field is not under the bar (input bottom ${boxes.inputBottom} <= bar top ${boxes.barTop})`, boxes.inputBottom <= boxes.barTop && boxes.inputTop >= 0);
      ok(`the bar is slim (${boxes.barH} px) and inside the window`, boxes.barH <= 64 && boxes.inside);
      ok("the bar sits above the tab bar", boxes.tabTop === null || boxes.barBottom <= boxes.tabTop + 1);
      eq("nothing sticks out with the bar", (await sticksOut()).join("; "), "");
      await ctx.click("#settings-save");
      await until(() => !document.getElementById("settings-bar").classList.contains("dirty"));
      ok("the bar is gone after Save", !(await barShown()));
      await settingsType(page, MAX_INPUT, "8");
      await until(() => document.getElementById("settings-bar").classList.contains("dirty"));
      await ctx.click("#settings-cancel");
      await until(() => document.getElementById("settings-confirm-modal").classList.contains("active"));
      await ctx.click("#settings-confirm-yes");
      await until(() => !document.getElementById("settings-bar").classList.contains("dirty"));
      ok("the bar is gone after Cancel", !(await barShown()));
    } finally { await page.evaluate(() => { const c = document.getElementById("settings-cancel"); if (c && !c.disabled) { c.click(); document.getElementById("settings-confirm-yes")?.click(); } }); } });
  }],

  ["1440 px: the bar is always there with Versions, Export, Import, Cancel and Save; the phone row and count are hidden", async ({ eq, goTab, ok, page, waitSome }) => {
    await goTab("settings");
    await waitSome(".set-sec", 8);
    const shown = (sel) => page.$eval(sel, (e) => getComputedStyle(e).display !== "none");
    ok("the bar shows with no change", await shown("#settings-bar"));
    ok("Versions, Export and Import are in it", await page.$$eval("#settings-bar .set-bar-l .btn", (b) => b.map((x) => x.offsetParent !== null && x.querySelector(".fl").offsetParent !== null).join()) === "true,true,true");
    ok("the phone row and the count are hidden", !(await shown(".set-tools")) && !(await shown("#settings-count")));
    eq("Save and Cancel are off", await page.$eval("#settings-bar", (b) => [...b.querySelectorAll("#settings-save, #settings-cancel")].map((x) => x.disabled).join()), "true,true");
  }],

  ["a theme switch keeps the tab readable (Nord), at desktop and phone width", async ({ eq, goTab, lookNow, noSideScroll, ok, page, phone, pickLook, waitSome }) => {
    await goTab("settings");
    await waitSome(".set-sec", 8);
    const before = await page.$eval(".set-sec", (e) => getComputedStyle(e).backgroundColor);
    await pickLook("nord");
    eq("Nord is on", await lookNow(), "nord");
    const after = await page.$eval(".set-sec", (e) => getComputedStyle(e).backgroundColor);
    ok("the cards took the theme's surface", before !== after);
    ok("no side scroll", await noSideScroll());
    await phone(async () => { await goTab("settings"); ok("no side scroll at 390 px", await noSideScroll()); });
    await pickLook("");
  }],

  ["no console errors, page errors or failed requests came from the tab", async (ctx) => {
    const { eq, problems } = ctx;
    eq("problems so far", problems.join(" | "), "");
    ctx.expectStatus();
  }],

  // The environment variant (test_ui_smoke.py "pinned": PENGUWATCH_TZ and PENGUWATCH_NOTIFY_URL are set): no fake needed.
  ["an environment-pinned setting is read-only and says which variable", async ({ eq, goTab, ok, page, waitSome }) => {
    await goTab("settings");
    await waitSome(".set-sec", 8);
    eq("the time zone box is disabled", await page.$eval("#set-in-server-timezone", (s) => s.disabled), true);
    ok("it says it comes from the environment", /From environment · PENGUWATCH_TZ/.test(await rowText(page, "server.timezone", "[data-meta]")));
    eq("the pinned value is what shows", await page.$eval("#set-in-server-timezone", (s) => s.value), "Europe/Berlin");
    ok("there is no Reset on it", await page.$('.set-row[data-key="server.timezone"] [data-action=settings-reset]') === null);
    ok("an unpinned setting is editable", await page.$eval("#set-in-notify-hour", (s) => !s.disabled));
  }, { variants: ["pinned"] }],

  ["a pinned secret has no buttons and shows neither its value nor its host", async ({ eq, ok, page }) => {
    const state = (await page.$eval('[data-slot="notify.url"]', (s) => s.textContent)).replace(/\s+/g, " ");
    ok(`it says where it comes from (${state.trim()})`, /From environment · PENGUWATCH_NOTIFY_URL/.test(state));
    eq("no Set, Replace or Remove", await page.$$eval('[data-slot="notify.url"] button', (b) => b.length), 0);
    ok("the address is nowhere in the page", !(await page.content()).includes("topic-secret"));
  }, { variants: ["pinned"] }],

  ["an invalid environment variable is ignored, said next to its setting, and the setting stays editable", async ({ ok, page }) => {
    const meta = await rowText(page, "server.log_level", "[data-meta]");
    ok(`the note is on the row (${meta})`, /PENGUWATCH_LOG_LEVEL is set but not valid[^]*ignored/.test(meta));
    ok("the note does not repeat the value", !/loud/i.test(meta));
    ok("the row stays editable", await page.$eval("#set-in-server-log-level", (s) => !s.disabled && !s.closest(".set-row").querySelector("[data-badge=env]")));
  }, { variants: ["pinned"] }],

  ["a webhook signing with an imported variable shows it read-only, with Stop using it", async (ctx) => {
    const { eq, ok, page, until } = ctx;
    const card = '.set-card[data-id="imported"]';
    const slot = `${card} [data-slot="webhook.imported.secret"]`;
    await page.$eval(card, (d) => { d.open = true; });
    ok("the slot names the variable", /From environment · PENGUWATCH_WEBHOOK_DEMO/.test(await ctx.text(`${slot} [data-state]`)));
    eq("the slot has no Set, Replace or Remove", await page.$$eval(`${slot} button`, (b) => b.length), 0);
    eq("there is no box to type a variable or a file into", await page.$$eval(`${card} input[data-wf=secret_env], ${card} input[data-wf=secret_file]`, (e) => e.length), 0);
    ok("the card says where the secret comes from", /read from the environment variable PENGUWATCH_WEBHOOK_DEMO/.test(await ctx.text(`${card} [data-imported]`)));
    await ctx.click(`${card} [data-action=settings-webhook-stop-import]`);
    await until((sel) => /Not set/.test(document.querySelector(`${sel} [data-state]`)?.textContent ?? ""), slot);
    ok("the stored secret can be set now", await page.$(`${slot} [data-action=settings-secret-replace]`) !== null);
    ok("the imported note is gone", await page.$(`${card} [data-imported]`) === null);
    await until(() => !document.getElementById("settings-save").disabled);
    await ctx.click("#settings-save");
    await until(() => /revision/.test(document.getElementById("settings-meta").textContent) && !/unsaved/.test(document.getElementById("settings-meta").textContent));
    const state = await page.evaluate(async () => (await fetch("/api/settings")).json());
    const item = state.lists.webhooks.items.find((i) => i.id === "imported");
    eq("the server no longer reads the variable", JSON.stringify([item.secret_env, item.secret_file, state.secrets["webhook.imported.secret"].source]), JSON.stringify([null, null, null]));
  }, { variants: ["pinned"] }],

  ["with an access token the no-token warning is gone", async ({ goTab, eq, waitSome, page }) => {
    await goTab("settings");
    await waitSome(".set-sec", 8);
    eq("no banner", await page.$("#settings-banner [data-banner]"), null);
  }, { variants: ["token"] }],
];
