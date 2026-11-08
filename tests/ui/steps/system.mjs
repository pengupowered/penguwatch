// System: sync health, latency and upstream calls of the app under test.
export const subject = "panel:system";
export const variants = ["demo"];

export const steps = [
  ["System shows its cards and the facts of this run", async ({ eq, goTab, noOldLabels, ok, page, text, waitSome }) => {
    await goTab("system");
    await waitSome("#system-container .panel-t", 5);
    eq("cards", (await page.$$eval("#system-container .panel-t", (e) => e.map((x) => x.textContent))).join(" | "),
      "Sync history | API latency by route | Upstream services | Event webhooks | Endpoints for your tools");
    const body = await text("#system-container");
    ok("sync is not connected in the demo", /SIMKL sync\s*Not connected/.test(body.replace(/\s+/g, " ")));
    ok("the version is shown", /Version\s*\d+\.\d+\.\d+/.test(body.replace(/\s+/g, " ")));
    ok("it warns that there is no access token (demo variant)", /No access token set/.test(body));
    ok("charts are drawn", (await page.$$eval("#system-container svg", (e) => e.length)) >= 1);
    await noOldLabels();
  }],
];
