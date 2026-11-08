// History: the last 7 days, 30 days or 12 months of the demo library's watching, against the period before.
export const subject = "panel:history";
export const variants = ["demo"];

const BARS = "#history-series path.hc-bar";
const BUTTON = (days) => `#history-period button[data-days="${days}"]`;
const METRICS = ["hours", "episodes", "movies", "titles_started", "titles_finished", "active_days"];

export const steps = [
  ["History opens on 30 days with its figures, change badges and every section", async ({ eq, goTab, ok, page, until, waitSome }) => {
    await goTab("history");
    await until(() => document.getElementById("history-container")?.dataset.days === "30");
    eq("pressed period", await page.$eval('#history-period [aria-pressed="true"]', (e) => e.textContent.trim()), "Last 30 days");
    eq("period button labels", (await page.$$eval("#history-period button", (e) => e.map((x) => x.textContent.trim()))).join(" | "), "Last 7 days | Last 30 days | Last 12 months");
    eq("figures", (await page.$$eval("#history-totals [data-metric]", (e) => e.map((x) => x.dataset.metric))).join(","), METRICS.join(","));
    const hours = await page.$eval('[data-metric="hours"] [data-role="value"]', (e) => e.textContent);
    ok(`hours are not zero (${hours})`, /^[1-9]\d*(\.\d)?h$/.test(hours));
    const dir = await page.$eval('[data-metric="hours"] [data-role="change"]', (e) => e.dataset.dir);
    ok(`the hours have a change badge (${dir})`, ["up", "down", "new", "same"].includes(dir));
    ok("the change shows an arrow or 'new'", await page.$eval('[data-metric="hours"] [data-role="change"]', (e) => !!e.querySelector("svg") || /new|no change/.test(e.textContent)));
    for (const id of ["series", "shows", "services", "genres", "weekdays", "hours"]) ok(`#history-${id} is there`, !!(await page.$(`#history-${id}`)));
    await waitSome("#history-shows .hist-show", 3);
    ok("at most 10 top shows", (await page.$$eval("#history-shows .hist-show", (e) => e.length)) <= 10);
    ok("genres listed", (await page.$$eval("#history-genres .hl-row", (e) => e.length)) >= 2);
    eq("weekday bars", await page.$$eval("#history-weekdays path.hc-bar", (e) => e.length), 7);
    eq("hour-of-day bars", await page.$$eval("#history-hours path.hc-bar", (e) => e.length), 24);
    ok("services show a cost per hour", /per hour/.test(await page.$eval("#history-services", (e) => e.textContent)));
    ok("the cost note says there is no record of past subscriptions", /no record of past subscriptions/.test(await page.$eval("#history-cost-note", (e) => e.textContent)));
  }],

  ["each period button redraws: 7 and 30 daily bars, at most 13 monthly bars with the partial ends marked", async ({ click, eq, ok, page, until }) => {
    const show = async (days) => { await click(BUTTON(days)); await until((d) => document.getElementById("history-container").dataset.days === d, String(days)); };
    await show(7);
    eq("7 days pressed", await page.$eval('#history-period [aria-pressed="true"]', (e) => e.dataset.days), "7");
    eq("7 daily bars", await page.$$eval(BARS, (e) => e.length), 7);
    await show(30);
    eq("30 daily bars", await page.$$eval(BARS, (e) => e.length), 30);
    await show(365);
    const partial = await page.$$eval(BARS, (e) => e.map((x) => x.dataset.partial));
    ok(`12 months: 13 monthly bars at most (${partial.length})`, partial.length >= 12 && partial.length <= 13);
    eq("first and last month are partial", `${partial[0]} ${partial[partial.length - 1]}`, "true true");
    ok("the months between are whole", partial.slice(1, -1).every((p) => p === "false"));
    for (const d of [7, 30, 365]) {
      await show(d);
      for (const m of ["hours", "episodes", "titles_started"]) ok(`${d} days: ${m} has a value`, (await page.$eval(`[data-metric="${m}"] [data-role="value"]`, (e) => e.textContent.trim())) !== "");
    }
  }],

  ["12 months: bulk-marked episodes are left out, said so in one line, and Include them counts them", async ({ click, eq, ok, page, until }) => {
    await click(BUTTON(365));
    await until(() => document.getElementById("history-container").dataset.days === "365" && document.getElementById("history-container").dataset.bulk === "false");
    const line = await page.$eval("#history-bulk", (e) => e.textContent.replace(/\s+/g, " ").trim());
    ok(`the bulk line (${line})`, /^\d+ episodes marked in bulk aren't counted as watching time\. Include them$/.test(line));
    const left = Number(/^(\d+)/.exec(line)[1]);
    const episodes = () => page.$eval('[data-metric="episodes"] [data-role="value"]', (e) => Number(e.textContent.replace(/\D/g, "")));
    const without = await episodes();
    await click("#history-bulk-toggle");
    await until(() => document.getElementById("history-container").dataset.bulk === "true");
    eq("with the bulk marks, the episodes grow by exactly those", await episodes(), without + left);
    eq("the line offers to leave them out", await page.$eval("#history-bulk-toggle", (e) => e.textContent.trim()), "Leave them out");
    await click("#history-bulk-toggle");
    await until(() => document.getElementById("history-container").dataset.bulk === "false");
    eq("back to the first count", await episodes(), without);
    await click(BUTTON(7));
    await until(() => document.getElementById("history-container").dataset.days === "7");
    ok("no bulk marks in the last 7 days: no line", !(await page.$("#history-bulk")));
  }],

  ["the chosen period survives a reload", async ({ click, eq, page, pageReady, until }) => {
    await click(BUTTON(365));
    await until(() => document.getElementById("history-container").dataset.days === "365");
    eq("stored", await page.evaluate(() => localStorage.getItem("history-days")), "365");
    await page.reload({ waitUntil: "load" });
    await pageReady();
    await until(() => document.getElementById("history-container")?.dataset.days === "365");
    eq("pressed after the reload", await page.$eval('#history-period [aria-pressed="true"]', (e) => e.dataset.days), "365");
    eq("the page's data follows", await page.$eval("#history-container", (e) => e.dataset.days), "365");
  }],

  ["g then h opens the tab", async ({ eq, goTab, page, until }) => {
    await goTab("upnext");
    await page.evaluate(() => document.activeElement?.blur());
    await page.keyboard.press("g");
    await page.keyboard.press("h");
    await until(() => document.querySelector(".nav-btn.active[data-route='history']") && document.getElementById("history-tab").classList.contains("active"));
    eq("hash", await page.evaluate(() => location.hash), "#/history");
  }],

  ["a period with nothing watched shows the empty state (the answer is stubbed: the demo always has viewing)", async ({ ok, page, until }) => {
    await page.setRequestInterception(true);
    const stub = (req) => {
      if (/\/api\/history\?/.test(req.url())) {
        const zero = { hours: 0, episodes: 0, movies: 0, titles_started: 0, titles_finished: 0, active_days: 0 };
        req.respond({ status: 200, contentType: "application/json", body: JSON.stringify({
          period: { days: 7, tz: "UTC" }, include_bulk: false, bulk_episodes: 0, bulk_groups: 0, weekday_start: "sunday",
          totals: { current: zero, previous: zero }, series: { unit: "day", buckets: [] }, top_shows: [], by_service: [], genres: [],
          weekdays: [0, 0, 0, 0, 0, 0, 0], hours_of_day: new Array(24).fill(0) }) });
      } else req.continue();
    };
    page.on("request", stub);
    try {
      await page.evaluate(() => document.querySelector('#history-period button[data-days="7"]').click());
      await until(() => !!document.getElementById("history-empty"));
      ok("the empty message names the period", /Nothing watched in the last 7 days/.test(await page.$eval("#history-empty", (e) => e.textContent)));
      ok("no charts are drawn", !(await page.$("#history-series")));
    } finally {
      page.off("request", stub);
      await page.setRequestInterception(false);
    }
  }],

  ["phone (390 px): every period fits without sideways scrolling", async ({ click, eq, noSideScroll, ok, page, phone, sticksOut, until }) => {
    await phone(async () => {
      await page.evaluate(() => { location.hash = "#/history"; });
      await until(() => document.getElementById("history-tab")?.classList.contains("active"));
      for (const days of [7, 30, 365]) {
        await click(BUTTON(days));
        await until((d) => document.getElementById("history-container").dataset.days === d, String(days));
        eq(`${days} days: sticks out`, JSON.stringify(await sticksOut()), "[]");
        ok(`${days} days: no sideways scroll`, await noSideScroll());
        ok(`${days} days: the page is no wider than the window`, await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1));
      }
    });
  }],

  ["dark theme: History renders with its charts and no errors", async ({ eq, flipMode, goTab, ok, page, setTheme, until }) => {
    await goTab("history");
    await until(() => !!document.querySelector("#history-totals"));
    await setTheme("dark");
    eq("theme", await page.evaluate(() => document.documentElement.dataset.theme), "dark");
    ok("the bars are drawn", (await page.$$eval(BARS, (e) => e.length)) > 0);
    const fill = await page.$eval(BARS, (e) => getComputedStyle(e).fill);
    ok(`bars have a colour (${fill})`, /^(rgb|color)/.test(fill));
    ok("tiles have a text colour that is not the page's", await page.$eval("#history-totals .val", (e) => getComputedStyle(e).color !== getComputedStyle(document.body).backgroundColor));
    await flipMode();
  }],
];
