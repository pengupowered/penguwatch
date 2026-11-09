// Dialog "details": a title's details and episode checklist, opened from Up Next.
export const subject = "dialog:details";
export const variants = ["demo"];

export const steps = [
  ["a title opened from Up Next shows its details, labels and episodes", async ({ eq, goTab, noOldLabels, ok, page, text, until, waitSome }) => {
    await goTab("upnext");
    await waitSome('#upnext-container [data-call="openShow"]');
    const name = await page.$eval("#upnext-container .upnext-card .title-link", (e) => e.textContent.trim());
    await page.evaluate(() => document.querySelector('#upnext-container [data-call="openShow"]').click());
    await until(() => document.querySelector("#episodes-modal.active #show-detail label"));
    ok(`the dialog is about the title clicked (${name})`, (await text("#show-detail")).includes(name));
    const labels = await page.$$eval("#show-detail label", (l) => l.map((x) => x.textContent.split("\n")[0].trim()));
    for (const want of [/^Status/, /^Your rating/, /^Priority/, /^Streams on/, /^Notes/]) ok(`a label ${want} (have: ${labels.join(" | ")})`, labels.some((l) => want.test(l)));
    // who's watching: a group of toggles (one per person), named by its heading
    eq("Who's watching, a toggle per person", await page.$eval("#show-detail .detail-who", (g) => `${g.querySelector(".detail-who-l").textContent}: ${[...g.querySelectorAll(".who-toggle")].map((t) => t.textContent.trim()).join(", ")}`), "Who's watching: Me, Partner");
    ok("its overview is the demo's", /invented title/.test(await text("#show-detail")));
    await waitSome("#episodes-body input, #episodes-body .ep-row, #episodes-body li");
    ok("the episode list is shown by season", /Season 1/.test(await text("#episodes-body")));
    ok("with episodes", /E01/.test(await text("#episodes-body")));
    eq("not left loading", /^Loading/.test((await text("#episodes-body")).trim()), false);
    await noOldLabels();
  }],

  ["the details dialog closes with Escape and gives the focus back", async ({ eq, modalOpen, page, until }) => {
    await page.keyboard.press("Escape");
    await until(() => !document.getElementById("episodes-modal").classList.contains("active"));
    eq("closed", await modalOpen("episodes-modal"), false);
  }],

  // `household` variant: after settings.mjs the people are Me, Sammy and Ana (Ana wants nothing yet)
  ["three people: tag a title for two of three in the details dialog; Up Next's Shared count and the Library filter follow", async ({ click, eq, goTab, ok, page, until, waitSome }) => {
    const chipCount = (key) => page.$eval(`#upnext-chips [data-chip="${key}"] .chip-count`, (e) => Number(e.textContent));
    await goTab("upnext");
    await waitSome("#upnext-container .upnext-card");
    const shared = await chipCount("both");
    const mine = await chipCount("m:person1");
    eq("Ana's chip is empty", await chipCount("m:person4"), 0);
    // a title only Me wants: Signal Lost
    await page.evaluate(() => [...document.querySelectorAll("#upnext-container .title-link")].find((a) => a.textContent.trim() === "Signal Lost").click());
    await until(() => document.querySelector("#episodes-modal.active .detail-who"));
    eq("the dialog's toggles", (await page.$$eval(".detail-who .who-toggle", (o) => o.map((x) => `${x.textContent.trim()}${x.querySelector("input").checked ? "*" : ""}`))).join(" | "), "Me* | Sammy | Ana");
    const patched = page.waitForResponse((r) => r.request().method() === "PATCH" && /\/api\/shows\/\d+$/.test(r.url()));
    await page.click(".detail-who .who-toggle:nth-child(3) input");
    eq("one PATCH with the whole set", JSON.parse((await patched).request().postData()).household.join(), "person1,person4");
    await page.keyboard.press("Escape");
    await until(() => !document.getElementById("episodes-modal").classList.contains("active"));
    await goTab("library");
    await goTab("upnext");
    await until((n) => Number(document.querySelector('#upnext-chips [data-chip="both"] .chip-count')?.textContent) === n, shared + 1);
    eq("Shared counts it", await chipCount("both"), shared + 1);
    eq("and Me's own chip one less", await chipCount("m:person1"), mine - 1);
    await goTab("library");
    await waitSome(".show-card");
    await page.select("#filter-household-select", "shared");
    await until(() => [...document.querySelectorAll(".show-card .show-title")].some((t) => t.textContent.includes("Signal Lost")));
    const chip = await page.$$eval(".show-card", (cards) => cards.find((c) => c.querySelector(".show-title").textContent.includes("Signal Lost")).querySelector(".who-chip").textContent.trim());
    eq("the Library's Shared filter shows it, for Me and Ana", chip, "Me, Ana");
    await page.select("#filter-household-select", "person4");
    await until(() => document.querySelectorAll(".show-card").length === 0);
    ok("Ana only: nothing (her one title is shared)", (await page.$$eval(".show-card", (c) => c.length)) === 0);
  }, { variants: ["household"] }],

  ["three people at 390 px: the dialog's who toggles fit beside each other, nothing sticks out", async ({ eq, goTab, layoutSettled, ok, page, phone, sticksOut, until, waitSome }) => {
    await goTab("upnext");
    await waitSome("#upnext-container .upnext-card");
    await phone(async () => {
      await page.evaluate(() => document.querySelector('#upnext-container [data-call="openShow"]').click());
      await until(() => document.querySelector("#episodes-modal.active .detail-who"));
      await layoutSettled();
      const out = await page.$$eval(".detail-who .who-toggle", (els) => els.filter((e) => { const r = e.getBoundingClientRect(); return r.left < 0 || r.right > innerWidth; }).length);
      eq("toggles outside the window", out, 0);
      ok("the toggles share a line", await page.$$eval(".detail-who .who-toggle", (els) => new Set(els.map((e) => Math.round(e.getBoundingClientRect().top))).size === 1));
      eq("sticks out", (await sticksOut()).join(" | "), "");
      await page.keyboard.press("Escape");
      await until(() => !document.getElementById("episodes-modal").classList.contains("active"));
    });
  }, { variants: ["household"] }],
];
