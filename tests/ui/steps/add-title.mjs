// Dialog "add-title": find a show or movie in the catalogue, or type it in by hand. Searching the catalogue needs SIMKL
// (and so the network), which the test has not, so this opens the dialog, checks its form and closes it, by button and by keyboard.
export const subject = "dialog:add-title";
export const variants = ["demo"];

export const steps = [
  ["Add title opens from the header with its search and manual form, and closes", async ({ click, dialogKeys, eq, goTab, modalOpen, ok, page, until }) => {
    await goTab("upnext");
    await click("#btn-header-add");
    await until(() => document.getElementById("add-show-modal").classList.contains("active"));
    eq("the search box has the focus", await page.evaluate(() => document.activeElement.id), "catalog-search-input");
    const hint = await page.$eval("#catalog-search-input", (i) => { const h = document.getElementById(i.getAttribute("aria-describedby")); return h ? { text: h.textContent.trim(), shown: h.getBoundingClientRect().height > 0 } : null; });
    // the catalogue's own links only when its driver takes them (/api/drivers takes_links): "SIMKL, " with the demo's drivers
    const links = await page.evaluate(async () => { const d = await (await fetch("/api/drivers")).json(); return d.catalog.filter((n) => d.drivers.find((x) => x.name === n)?.takes_links).map((n) => `${d.drivers.find((x) => x.name === n).label}, `).join(""); });
    ok(`the demo's catalogue takes links (${links})`, links.length > 0);
    eq("the search box's hint says which links or ids can be pasted", hint?.shown && hint.text, `Or paste a link (${links}IMDb or TMDB) or an IMDb id (tt…).`);
    eq("search types", (await page.$$eval("#catalog-search-type option", (o) => o.map((x) => x.textContent))).join(" | "), "All | TV | Anime | Movies");
    eq("the manual form's required field", await page.$eval("#m-title", (i) => i.required), true);
    eq("who wants to watch: a toggle per person, the first ticked", (await page.$$eval("#m-household .who-toggle", (o) => o.map((x) => `${x.textContent.trim()}${x.querySelector("input").checked ? "*" : ""}`))).join(" | "), "Me* | Partner");
    ok("Add is a submit button", await page.$eval("#manual-add-form button[type=submit]", (b) => b.getBoundingClientRect().width > 0));
    await click("#btn-close-add-modal");
    await until(() => !document.getElementById("add-show-modal").classList.contains("active"));
    eq("closed with its button", await modalOpen("add-show-modal"), false);
    await dialogKeys("#btn-header-add", "add-show-modal"); // Tab stays inside, Escape closes, focus returns to the opener
  }],

  ["Add title also opens from the Library's button", async ({ click, eq, goTab, modalOpen, until }) => {
    await goTab("library");
    await click("#btn-add-show-modal");
    await until(() => document.getElementById("add-show-modal").classList.contains("active"));
    await click("#btn-close-add-modal");
    await until(() => !document.getElementById("add-show-modal").classList.contains("active"));
    eq("closed", await modalOpen("add-show-modal"), false);
  }],

  ["Searching shows the catalogue's answer in the order given, the exact match first (the answer is stubbed: the demo has no catalogue)", async ({ click, eq, goTab, ok, page, until }) => {
    const item = (title, year, id) => ({ ids: { simkl: id }, media_type: "show", title, year, poster: null, overview: null, network: null, genres: [], status: null, ratings: {}, service_id: null, service_name: "Unknown", in_library: false });
    const answer = [item("Saturday Night Live", 1975, 5767), item("Saturday Night Live", 2013, 900), item("SNL50", 2025, 1), item("SNL Digital Shorts", 2005, 2)];
    let asked = "";
    await page.setRequestInterception(true);
    const stub = (req) => {
      if (/\/api\/catalog\/search\?/.test(req.url())) {
        asked = new URL(req.url()).searchParams.get("q");
        req.respond({ status: 200, contentType: "application/json", body: JSON.stringify(answer) });
      } else req.continue();
    };
    page.on("request", stub);
    try {
      await goTab("upnext");
      await click("#btn-header-add");
      await until(() => document.getElementById("add-show-modal").classList.contains("active"));
      await page.type("#catalog-search-input", "Saturday Night Live");
      await page.keyboard.press("Enter");
      await until(() => document.querySelectorAll("#catalog-search-results .search-item").length === 4);
      eq("what was asked", asked, "Saturday Night Live");
      const first = await page.$eval("#catalog-search-results .search-item .search-item-title", (e) => e.textContent.replace(/\s+/g, " ").trim());
      ok("the exact match (the 1975 original) is listed first: " + first, /Saturday Night Live \(1975\)/.test(first));
      eq("all four are listed", await page.$$eval("#catalog-search-results .search-item", (e) => e.length), 4);
      await click("#btn-close-add-modal");
      await until(() => !document.getElementById("add-show-modal").classList.contains("active"));
    } finally {
      page.off("request", stub);
      await page.setRequestInterception(false);
    }
  }],
];
