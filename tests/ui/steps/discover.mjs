// Discover: trending and personal picks. The demo library has no SIMKL login and the test has no network, so there is no
// list, only the panel's "no discovery source" message; what is checked is that the panel, its controls and the message
// are there, and that the page does not ask the server for a list it cannot get.
export const subject = "panel:discover";
export const variants = ["demo"];

export const steps = [
  ["Discover shows its controls and says why there is no list (SIMKL is not connected)", async ({ eq, goTab, noOldLabels, ok, page, text, until }) => {
    await goTab("discover");
    await until(() => !document.querySelector("#discover-container .loading-state"));
    eq("the lists", (await page.$$eval("#discover-mode .filter-tab", (b) => b.map((x) => x.textContent.trim()))).join(" | "), "Trending | For you");
    eq("the media types", (await page.$$eval("#discover-media option", (o) => o.map((x) => x.textContent))).join(" | "), "TV Shows | Movies | Anime");
    eq("the periods", (await page.$$eval("#discover-interval option", (o) => o.map((x) => x.textContent))).join(" | "), "Today | This Week | This Month");
    eq("hiding library titles is on", await page.$eval("#discover-hide-library", (c) => c.checked), true);
    ok(`the message (${(await text("#discover-container")).trim()})`, /discovery source/i.test(await text("#discover-container")));
    await noOldLabels();
  }],

  ["without a login the page makes no request to /api/discover/*", async ({ base, eq, ok, page, pageReady, sleep, text }) => {
    const asked = [];
    const watch = (r) => { if (new URL(r.url()).pathname.startsWith("/api/discover/")) asked.push(r.url()); };
    page.on("request", watch);
    try {
      await page.goto(base + "/#/discover", { waitUntil: "load" }); // Discover is the first tab shown
      await pageReady();
      await sleep(1000); // the login state arrives after the page loads; the panel looks again then
      await page.click('#discover-mode [data-mode="foryou"]');
      await sleep(500);
      await page.click('#discover-mode [data-mode="trending"]');
      await page.select("#discover-media", "movies");
      await sleep(500);
    } finally {
      page.off("request", watch);
    }
    eq("requests to /api/discover/", asked.join(" "), "");
    ok("and the message is shown", /discovery source/i.test(await text("#discover-container")));
  }],
];
