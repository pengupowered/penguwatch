// Ratings: the built-in and the demo library's two custom rating sources, and the CSV import form.
export const subject = "panel:ratings";
export const variants = ["demo"];

export const steps = [
  ["Ratings lists the built-in and custom sources with their title counts", async ({ eq, goTab, noOldLabels, ok, page, waitSome }) => {
    await goTab("ratings");
    await waitSome("#rating-sources-list tbody tr", 4);
    const rows = await page.$$eval("#rating-sources-list tbody tr", (els) => els.map((e) => e.textContent.replace(/\s+/g, " ").trim()));
    for (const name of ["SIMKL", "IMDb", "Critics' circle", "Weekend club"]) ok(`a row for ${name} (${rows.map((r) => r.slice(0, 20)).join(" | ")})`, rows.some((r) => r.startsWith(name)));
    ok("the custom sources are marked as yours", rows.filter((r) => /Yours/.test(r)).length === 2);
    ok("built-in sources count their titles", /\b2\d\b/.test(rows.find((r) => r.startsWith("SIMKL"))));
    eq("the New source button", await page.$eval('[data-call="openRatingSource"]', (b) => b.textContent.trim()), "New source");
    await noOldLabels();
  }],

  ["the CSV import offers every source and waits for a file", async ({ eq, ok, page }) => {
    const options = await page.$$eval("#import-source option", (o) => o.map((x) => x.textContent));
    ok(`the import targets the custom sources (${options.join(" | ")})`, options.some((o) => o.includes("Critics")) && options.some((o) => o.includes("Weekend")));
    eq("Import is disabled until a file is chosen", await page.$eval("#btn-run-import", (b) => b.disabled), true);
  }],
];
