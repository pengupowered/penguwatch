// Up Next: the next episode of every show in progress, on the demo library (scripts/demo_library.py).
export const subject = "panel:upnext";
export const variants = ["demo"];

export const steps = [
  ["Up Next shows the demo library's chips, stats and one card per show in progress", async ({ eq, goTab, noOldLabels, ok, page, waitSome }) => {
    await goTab("upnext");
    await waitSome("#upnext-container .upnext-card");
    const chips = await page.$$eval("#upnext-chips .upnext-chip", (els) => els.map((e) => ({ key: e.dataset.chip, n: Number(e.querySelector(".chip-count")?.textContent ?? NaN) })));
    ok(`the chips (${chips.map((c) => c.key).join(", ")}) include All, On your services, Aired today and Both want`, ["all", "active", "today", "both"].every((k) => chips.some((c) => c.key === k)));
    const cards = await page.$$eval("#upnext-container .upnext-card", (els) => els.length);
    ok("the demo library has shows in progress", cards >= 5);
    eq("the All chip counts the cards", chips.find((c) => c.key === "all").n, cards);
    eq("three stat chips", await page.$$eval("#upnext-stats .stat-chip", (els) => els.length), 3);
    const card = await page.$eval("#upnext-container .upnext-card", (c) => ({
      title: c.querySelector(".upnext-title")?.textContent.trim(), code: c.querySelector(".upnext-code")?.textContent.trim(),
      poster: (() => { const i = c.querySelector("img.upnext-poster"); return !!i && i.complete && i.naturalWidth > 0; })(),
      open: !!c.querySelector('[data-call="openShow"]'),
    }));
    ok(`a card has a title (${card.title})`, !!card.title);
    ok(`and an episode code (${card.code})`, /^S\d+E\d+$/.test(card.code ?? ""));
    eq("its poster (a data: URL of the demo library) is drawn", card.poster, true);
    eq("it opens the details dialog", card.open, true);
    await noOldLabels();
  }],

  ["the Both want chip narrows the list to the count it shows, All brings it back", async ({ click, eq, ok, page, until, waitSome }) => {
    const count = () => page.$$eval("#upnext-container .upnext-card", (els) => els.length);
    const all = await count();
    const both = await page.$eval('#upnext-chips [data-chip="both"] .chip-count', (e) => Number(e.textContent));
    ok(`some, not all, shows are wanted by both (${both} of ${all})`, both > 0 && both < all);
    await click('#upnext-chips [data-chip="both"]');
    await until((n) => document.querySelectorAll("#upnext-container .upnext-card").length === n, both);
    eq("Both want is pressed", await page.$eval('#upnext-chips [data-chip="both"]', (e) => e.getAttribute("aria-pressed")), "true");
    await click('#upnext-chips [data-chip="all"]');
    await waitSome("#upnext-container .upnext-card", all);
    eq("All again", await count(), all);
  }],
];
