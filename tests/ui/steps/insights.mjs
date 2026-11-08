// Insights: what the demo library's watch history adds up to.
export const subject = "panel:insights";
export const variants = ["demo"];

export const steps = [
  ["Insights shows its figures and charts from the demo watch history", async ({ eq, goTab, noOldLabels, ok, page, waitSome }) => {
    await goTab("insights");
    await waitSome("#insights-container .tile", 5);
    eq("tile labels", (await page.$$eval("#insights-container .tile .lbl", (e) => e.map((x) => x.textContent))).join(" | "),
      "Watched in 12 months | Per month on average | Days with viewing, last 90 | Finish rate | Busiest weekday");
    const watched = await page.$eval("#insights-container .tile .val", (e) => e.textContent.trim());
    ok(`hours watched are not zero (${watched})`, /^[1-9]\d*h$/.test(watched));
    eq("chart panels", (await page.$$eval("#insights-container .panel-t", (e) => e.map((x) => x.textContent))).join(" | "), "Hours watched per month | Hours by service | Top genres | Weekday rhythm");
    ok("bar charts are drawn", (await page.$$eval("#insights-container svg.ch-svg path.ch-bar", (e) => e.length)) >= 6);
    ok("each chart has a table alternative", (await page.$$eval("#insights-container details.ch-table", (e) => e.length)) >= 2);
    await noOldLabels();
  }],

  ["the period selector redraws the figures for the period", async ({ eq, ok, page, until }) => {
    const twelve = await page.$eval("#insights-container .tile .val", (e) => e.textContent.trim());
    await page.select("#insights-months", "6");
    await until(() => document.querySelector("#insights-container .tile .lbl")?.textContent === "Watched in 6 months");
    const six = await page.$eval("#insights-container .tile .val", (e) => e.textContent.trim());
    ok(`six months are not more than twelve (${six} vs ${twelve})`, parseFloat(six) <= parseFloat(twelve));
    eq("the bars follow the period", await page.$eval("#insights-container svg.ch-svg", (svg) => svg.querySelectorAll("path.ch-bar").length), 6);
  }],
];
