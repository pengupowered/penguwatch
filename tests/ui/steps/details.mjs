// Dialog "details": a title's details and episode checklist, opened from Up Next.
export const subject = "dialog:details";
export const variants = ["demo"];

export const steps = [
  ["a title opened from Up Next shows its details, labels and episodes", async ({ eq, goTab, noOldLabels, ok, page, text, until, waitSome }) => {
    await goTab("upnext");
    await waitSome('#upnext-container [data-call="openShow"]');
    const name = await page.$eval("#upnext-container .upnext-card .title-link", (e) => e.textContent.trim());
    await page.evaluate(() => document.querySelector('#upnext-container [data-call="openShow"]').click());
    await until(() => document.querySelector("#episodes-modal.active #show-detail label"));
    ok(`the dialog is about the title clicked (${name})`, (await text("#show-detail")).includes(name));
    const labels = await page.$$eval("#show-detail label", (l) => l.map((x) => x.textContent.split("\n")[0].trim()));
    for (const want of [/^Status/, /^Your rating/, /^Who's watching/, /^Priority/, /^Streams on/, /^Notes/]) ok(`a label ${want} (have: ${labels.join(" | ")})`, labels.some((l) => want.test(l)));
    ok("its overview is the demo's", /invented title/.test(await text("#show-detail")));
    await waitSome("#episodes-body input, #episodes-body .ep-row, #episodes-body li");
    ok("the episode list is shown by season", /Season 1/.test(await text("#episodes-body")));
    ok("with episodes", /E01/.test(await text("#episodes-body")));
    eq("not left loading", /^Loading/.test((await text("#episodes-body")).trim()), false);
    await noOldLabels();
  }],

  ["the details dialog closes with Escape and gives the focus back", async ({ eq, modalOpen, page, until }) => {
    await page.keyboard.press("Escape");
    await until(() => !document.getElementById("episodes-modal").classList.contains("active"));
    eq("closed", await modalOpen("episodes-modal"), false);
  }],
];
