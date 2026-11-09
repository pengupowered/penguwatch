// Library (panel id "watchlist"): poster cards of the demo library with the household filter, chips and sort options.
export const subject = "panel:watchlist";
export const variants = ["demo"];

export const steps = [
  ["Library shows the demo titles as cards with a household chip each", async ({ eq, goTab, noOldLabels, ok, page, text, waitSome }) => {
    await goTab("library");
    await waitSome(".show-card", 5);
    const cards = await page.$$eval(".show-card", (els) => els.length);
    eq("the count line matches the cards", (await text("#library-count")).trim(), `${cards} titles`);
    eq("a household chip on every card", await page.$$eval('.show-card [data-field="household"]', (els) => els.length), cards);
    ok("a card has a title that opens the details", (await page.$$eval(".show-card .show-title[data-call=openShow]", (els) => els.length)) === cards);
    eq("the default filter is Watching", await page.$eval(".status-filters .filter-tab.active", (e) => e.dataset.status), "watching");
    await noOldLabels();
  }],

  ["the household filter has its options; the chip editor offers a toggle per person", async ({ eq, click, page, until }) => {
    eq("household filter options", (await page.$$eval("#filter-household-select option", (o) => o.map((x) => x.textContent))).join(" | "), "Anyone | Me only | Partner only | Both want");
    await click('.show-card [data-field="household"]');
    await until(() => document.querySelector(".who-editor .who-toggle"));
    eq("chip editor toggles", (await page.$$eval(".who-editor .who-toggle", (o) => o.map((x) => x.textContent.trim()))).join(" | "), "Me | Partner");
    await page.keyboard.press("Escape"); // closes without saving
    await until(() => !document.querySelector(".who-editor"));
  }],

  ["the sort options include the custom rating sources; Title (A-Z) orders the cards", async ({ eq, ok, page, until }) => {
    const options = await page.$$eval("#sort-by-select option", (o) => o.map((x) => x.textContent));
    for (const want of ["Priority & Rating", "Title (A-Z)", "Next Air Date", "Recently Watched", "Your Rating", "Likely interest", "Highest Critics' circle", "Highest Weekend club"]) {
      ok(`sort option "${want}" (have: ${options.join(" | ")})`, options.includes(want));
    }
    await page.select("#sort-by-select", "title");
    // the title is the heading's own text (its icon and year are other nodes)
    const titles = () => page.$$eval(".show-card .show-title", (els) => els.map((e) => [...e.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join("").trim()));
    await until(() => document.querySelector(".show-card .show-title")?.textContent.includes("Brass Meridian"));
    const got = await titles();
    eq("A-Z", got.join("|"), [...got].sort((a, b) => a.localeCompare(b)).join("|"));
  }],

  ["the status tabs and the search box narrow the cards", async ({ click, eq, ok, page, until, waitSome }) => {
    const watching = await page.$$eval(".show-card", (els) => els.length);
    await click('.status-filters [data-status=""]');
    await waitSome(".show-card", watching + 1);
    const all = await page.$$eval(".show-card", (els) => els.length);
    ok(`All shows more than Watching (${all} > ${watching})`, all > watching);
    await page.type("#watchlist-search", "Harbor");
    await until(() => document.querySelectorAll(".show-card").length === 1);
    eq("the search finds the one title", await page.$eval(".show-card .show-title", (e) => e.textContent.includes("The Quiet Harbor")), true);
  }],
  ["Library cards: the service chip on the poster shows its whole name beside three rating badges, at 1440 and 1280 px (default and video-store)", async ({ DESKTOP, click, eq, layoutSettled, ok, page, pageReady, pickLook, sleep, until, waitSome }) => {
    // every title of the demo, not just one: no search, the All tab
    await page.$eval("#watchlist-search", (i) => { i.value = ""; i.dispatchEvent(new Event("input", { bubbles: true })); });
    await click('.status-filters [data-status=""]');
    await waitSome(".show-card", 5);
    const measure = () => page.$$eval(".show-card", (cards) => cards.map((c) => {
      const chip = c.querySelector(".poster-top-badges .service-tag");
      const box = chip.getBoundingClientRect(), row = chip.parentElement.getBoundingClientRect();
      // cut: text wider than the chip (the ellipsis), or the chip past the poster's edge
      return { name: chip.textContent.trim(), cut: chip.scrollWidth > chip.clientWidth || box.right > row.right + 0.5 || box.width < 1, badges: c.querySelectorAll(".on-image-ratings .rating").length };
    }));
    for (const look of ["", "video-store"]) {
      await pickLook(look);
      for (const width of [1440, 1280]) {
        await page.setViewport({ width, height: 1000 });
        await pageReady();
        await layoutSettled();
        await sleep(200);
        const cards = await measure();
        const what = `${look || "default"} at ${width}`;
        ok(`${what}: some card has three rating badges (${Math.max(...cards.map((c) => c.badges))} at most, ${cards.length} cards)`, cards.some((c) => c.badges >= 3));
        eq(`${what}: service chips cut short`, cards.filter((c) => c.cut).map((c) => c.name).join(" | "), "");
      }
    }
    await pickLook("");
    await page.setViewport(DESKTOP);
    await pageReady();
    await click('.status-filters [data-status="watching"]');
    await until(() => document.querySelector('.status-filters .filter-tab.active')?.dataset.status === "watching");
  }],

  // `household` variant (test_ui_smoke.py): the demo library with three people (scripts/demo_library.py --people 3)
  ["three people: the Audience filter has each person and Shared; the cards name one person, two, or Everyone", async ({ click, eq, goTab, ok, page, until, waitSome }) => {
    await goTab("library");
    await click('.status-filters [data-status=""]');
    await waitSome(".show-card", 10);
    eq("household filter options", (await page.$$eval("#filter-household-select option", (o) => o.map((x) => x.textContent))).join(" | "), "Anyone | Me only | Partner only | Sam only | Shared");
    const chips = async () => page.$$eval(".show-card .who-chip", (els) => els.map((e) => e.textContent.trim()));
    const all = await chips();
    for (const form of ["Me", "Partner", "Sam", "Everyone", "Me, Sam", "Partner, Sam", "Me, Partner"]) ok(`a card reads "${form}"`, all.includes(form));
    await page.select("#filter-household-select", "person3");
    await until(() => [...document.querySelectorAll(".show-card .who-chip")].every((c) => c.textContent.trim() === "Sam") && document.querySelectorAll(".show-card").length > 0);
    eq("Sam only: every card is Sam's alone", [...new Set(await chips())].join(), "Sam");
    await page.select("#filter-household-select", "shared");
    await until(() => [...document.querySelectorAll(".show-card .who-chip")].every((c) => /, |Everyone/.test(c.textContent)));
    ok("Shared: every card names two people or Everyone", (await chips()).every((c) => c === "Everyone" || c.includes(", ")));
    await page.select("#filter-household-select", "");
    await waitSome(".show-card", all.length);
  }, { variants: ["household"] }],

  ["three people: a card's who editor has a toggle each, keeps one ticked, and saves the set when done", async ({ click, eq, ok, page, until }) => {
    await page.type("#watchlist-search", "Signal Lost");
    await until(() => document.querySelectorAll(".show-card").length === 1);
    await click('.show-card [data-field="household"]');
    await until(() => document.querySelector(".who-editor .who-toggle"));
    eq("toggles, Me ticked", (await page.$$eval(".who-editor .who-toggle", (o) => o.map((x) => `${x.textContent.trim()}${x.querySelector("input").checked ? "*" : ""}`))).join(" | "), "Me* | Partner | Sam");
    await page.click(".who-editor .who-toggle:nth-child(1) input");
    ok("the last ticked person stays ticked", await page.$eval(".who-editor .who-toggle:nth-child(1) input", (i) => i.checked));
    await page.click(".who-editor .who-toggle:nth-child(3) input");
    await click(".who-editor .who-done");
    await until(() => document.querySelector(".show-card .who-chip")?.textContent.trim() === "Me, Sam");
    const saved = await page.evaluate(async () => (await (await fetch("/api/shows?search=Signal%20Lost")).json())[0].household);
    eq("saved as one change", saved.join(), "person1,person3");
    await click('.show-card [data-field="household"]');
    await until(() => document.querySelector(".who-editor .who-toggle"));
    await page.click(".who-editor .who-toggle:nth-child(3) input");
    await click(".who-editor .who-done");
    await until(() => document.querySelector(".show-card .who-chip")?.textContent.trim() === "Me");
  }, { variants: ["household"] }],
];
