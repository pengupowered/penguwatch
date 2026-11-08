// Tonight's Pick: filters and one pick from the demo library's backlog.
export const subject = "panel:decide";
export const variants = ["demo"];

export const steps = [
  ["Tonight's Pick has its filters and the audience options of the household", async ({ eq, goTab, noOldLabels, page }) => {
    await goTab("pick");
    eq("audience options", (await page.$$eval("#decide-audience option", (o) => o.map((x) => x.textContent))).join(" | "), "Both want to watch | Me's pick | Partner's pick | Anyone's pick");
    eq("the rating filter defaults to 8.0+", await page.$eval("#decide-rating", (s) => s.value), "8.0");
    eq("no result before the button is pressed", await page.$eval("#decide-result", (e) => e.hidden), true);
    await noOldLabels();
  }],

  ["Pick for us tonight shows a winner from the library", async ({ click, ok, page, text, until }) => {
    await page.select("#decide-service", "all");
    await page.select("#decide-audience", "all");
    await page.select("#decide-rating", "0");
    await click("#btn-spin-roulette");
    await until(() => !document.getElementById("decide-result").hidden);
    const title = (await text("#result-title")).trim();
    ok(`a real title, not the placeholder (${title})`, title !== "" && title !== "Show Title");
    ok("it has an overview made up by the demo library", /invented title/.test(await text("#result-overview")));
    ok("and a poster", await page.$eval("#result-poster", (i) => i.complete && i.naturalWidth > 0));
    ok("and a way to go on", await page.$eval("#btn-spin-again", (b) => b.getBoundingClientRect().width > 0));
  }],
];
