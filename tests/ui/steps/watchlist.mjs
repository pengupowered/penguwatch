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

  ["the household filter has its options; the chip editor offers the members and Both", async ({ eq, click, page, until }) => {
    eq("household filter options", (await page.$$eval("#filter-household-select option", (o) => o.map((x) => x.textContent))).join(" | "), "All Audiences | Both Want | Me Only | Partner Only");
    await click('.show-card [data-field="household"]');
    await until(() => document.querySelector(".chip-editor option"));
    eq("chip editor options", (await page.$$eval(".chip-editor option", (o) => o.map((x) => x.textContent))).join(" | "), "Me | Partner | Both");
    await page.evaluate(() => document.activeElement.blur()); // the editor closes when it loses the focus (Escape is taken by the open picker)
    await until(() => !document.querySelector(".chip-editor"));
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
];
