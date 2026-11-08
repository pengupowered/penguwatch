// Optimizer: what the demo library's subscriptions cost and what to do about each.
export const subject = "panel:optimizer";
export const variants = ["demo"];

export const steps = [
  ["Optimizer shows the spend summary and a row per active subscription", async ({ eq, goTab, noOldLabels, ok, page, text, waitSome }) => {
    await goTab("optimizer");
    await waitSome("#opt-subs-table tbody tr");
    ok(`the summary says what you pay (${(await text("#opt-summary")).replace(/\s+/g, " ").slice(0, 80)})`, /You pay\s*\$\d+\.\d\d\/mo/.test(await text("#opt-summary")));
    ok("and what you could save", /Could save\s*\$\d+\.\d\d\/mo/.test(await text("#opt-summary")));
    // demo_library.ACTIVE: five active services
    eq("a row per active subscription", await page.$$eval("#opt-subs-table tbody tr", (els) => els.length), 5);
    const spend = await text("#head-spend");
    ok(`the header's spend matches the summary (${spend})`, (await text("#opt-summary")).includes(spend));
    await noOldLabels();
  }],

  ["What to subscribe to next lists ranked services; the rotation plan opens", async ({ click, ok, page, until, waitSome }) => {
    await click("#opt-next-details > summary");
    await until(() => document.getElementById("opt-next-details").open);
    await waitSome("#optimizer-rankings-container > *");
    ok("services are ranked", (await page.$$eval("#optimizer-rankings-container > *", (els) => els.length)) >= 2);
    await click("#opt-plan-details > summary");
    await until(() => document.getElementById("opt-plan-details").open);
    ok("the plan has controls", (await page.$$eval("#planner-section select, #planner-section button", (els) => els.length)) >= 1);
  }],
];
