// Air Schedule: upcoming episodes of the demo library (dates relative to today) and the calendar feed address.
export const subject = "panel:calendar";
export const variants = ["demo"];

export const steps = [
  ["Air Schedule lists upcoming episodes and shows the calendar feed address", async ({ base, eq, goTab, noOldLabels, ok, page, text, waitSome }) => {
    await goTab("schedule");
    await waitSome("#calendar-timeline .timeline-card", 5);
    const feed = (await text("#ical-url")).trim();
    ok(`the feed address is on this server (${feed})`, feed.startsWith(base) && feed.endsWith("/api/calendar.ics"));
    const card = await page.$eval("#calendar-timeline .timeline-card", (c) => c.textContent.replace(/\s+/g, " ").trim());
    ok(`a card names an episode (${card.slice(0, 60)})`, /S\d+E\d+/.test(card));
    eq("the feed itself is served as a calendar", await page.evaluate(async () => (await fetch("/api/calendar.ics")).headers.get("content-type")?.split(";")[0]), "text/calendar");
    await noOldLabels();
  }],
];
