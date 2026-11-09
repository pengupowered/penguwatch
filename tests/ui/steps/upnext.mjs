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
  ["phone (390 px): the filter chips wrap inside the window, every chip whole (default, synthwave and amber-crt)", async ({ eq, goTab, layoutSettled, page, phone, pickLook, sleep }) => {
    await goTab("upnext");
    for (const look of ["", "synthwave", "amber-crt"]) {
      await pickLook(look);
      await phone(async () => {
        await layoutSettled();
        await sleep(200);
        const m = await page.$eval("#upnext-chips", (row) => {
          const box = row.getBoundingClientRect();
          return { scrolls: row.scrollWidth > row.clientWidth + 0.5, chips: [...row.querySelectorAll(".upnext-chip")].map((c) => { const r = c.getBoundingClientRect(); return { text: c.textContent.replace(/\s+/g, " ").trim(), out: r.left < Math.max(0, box.left) - 0.5 || r.right > Math.min(innerWidth, box.right) + 0.5 || c.scrollWidth > c.clientWidth + 0.5 }; }) };
        });
        eq(`${look || "default"}: the chip row scrolls sideways`, m.scrolls, false);
        eq(`${look || "default"}: chips cut or outside the window`, m.chips.filter((c) => c.out).map((c) => c.text).join(" | "), "");
        eq(`${look || "default"}: six chips (All, On your services, Aired today, one per person, Both want)`, m.chips.length, 6);
      });
    }
    await pickLook("");
  }],

  // `household` variant (test_ui_smoke.py): the demo library with three people (scripts/demo_library.py --people 3)
  ["three people: a chip per person (the titles they want alone) and Shared; together they count every card", async ({ click, eq, goTab, ok, page, until, waitSome }) => {
    await goTab("upnext");
    await waitSome("#upnext-container .upnext-card");
    const chips = await page.$$eval("#upnext-chips .upnext-chip", (els) => els.map((e) => ({ key: e.dataset.chip, label: e.querySelector(".chip-l").textContent, n: Number(e.querySelector(".chip-count")?.textContent ?? NaN) })));
    eq("the chips", chips.map((c) => `${c.key}:${c.label}`).join(" | "), "all:All | active:On your services | today:Aired today | m:person1:Me | m:person2:Partner | m:person3:Sam | both:Shared");
    const n = (key) => chips.find((c) => c.key === key).n;
    eq("each title is one person's or shared: the counts add up to All", n("m:person1") + n("m:person2") + n("m:person3") + n("both"), n("all"));
    ok(`some titles are shared (${n("both")})`, n("both") > 0);
    await click('#upnext-chips [data-chip="m:person3"]');
    await until((k) => document.querySelectorAll("#upnext-container .upnext-card").length === k, n("m:person3"));
    eq("Sam's cards say Sam", [...new Set(await page.$$eval("#upnext-container .upnext-meta", (els) => els.map((e) => e.textContent.split(" · ").pop().trim())))].join(), "Sam");
    await click('#upnext-chips [data-chip="both"]');
    await until((k) => document.querySelectorAll("#upnext-container .upnext-card").length === k, n("both"));
    const who = await page.$$eval("#upnext-container .upnext-meta", (els) => els.map((e) => e.textContent.split(" · ").pop().trim()));
    ok(`the shared cards name two people or Everyone (${who.join(" | ")})`, who.every((w) => w === "Everyone" || w.includes(", ")));
    await click('#upnext-chips [data-chip="all"]');
  }, { variants: ["household"] }],

  ["three people at 390 px: the seven chips wrap inside the window, every chip whole, nothing sticks out", async ({ eq, goTab, layoutSettled, page, phone, sticksOut }) => {
    await goTab("upnext");
    await phone(async () => {
      await layoutSettled();
      const cut = await page.$$eval("#upnext-chips .upnext-chip", (els) => els.filter((c) => { const r = c.getBoundingClientRect(); return r.left < 0 || r.right > innerWidth || c.scrollWidth > c.clientWidth + 0.5; }).map((c) => c.textContent.trim()));
      eq("chips cut or outside the window", cut.join(" | "), "");
      eq("sticks out", (await sticksOut()).join(" | "), "");
    });
  }, { variants: ["household"] }],
];
