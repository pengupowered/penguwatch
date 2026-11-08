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
    eq("search types", (await page.$$eval("#catalog-search-type option", (o) => o.map((x) => x.textContent))).join(" | "), "All | TV | Anime | Movies");
    eq("the manual form's required field", await page.$eval("#m-title", (i) => i.required), true);
    eq("who wants to watch (household members)", (await page.$$eval("#m-household option", (o) => o.map((x) => x.textContent))).join(" | "), "Me | Both want to watch | Partner");
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
];
