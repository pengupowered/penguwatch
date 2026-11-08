// Services & Sync: the subscriptions of the demo library and the SIMKL box (not connected in the demo variant).
export const subject = "panel:services";
export const variants = ["demo"];

export const steps = [
  ["Services lists the streaming services, the demo's five active, and a form to add one", async ({ eq, goTab, noOldLabels, ok, page, waitSome }) => {
    await goTab("services");
    await waitSome("#services-manager-list .svc-item", 10);
    eq("active subscriptions (demo_library.ACTIVE)", await page.$$eval("#services-manager-list .svc-item.is-active", (els) => els.length), 5);
    ok("Netflix is one of them", await page.$$eval("#services-manager-list .svc-item.is-active .svc-item-title", (els) => els.some((e) => e.textContent.includes("Netflix"))));
    for (const id of ["add-service-name", "add-service-price", "add-service-aliases", "add-service-titles", "btn-add-service"]) ok(`#${id} is in the form`, await page.$(`#${id}`) !== null);
    await noOldLabels();
  }],

  ["the SIMKL box says it is not connected and offers to connect", async ({ ok, page, text }) => {
    ok(`the status (${(await text("#library-box-word")).trim()})`, /not connected/i.test(await text("#library-status-box")));
    ok("the connect button is shown", await page.$eval("#btn-start-connect", (b) => b.getBoundingClientRect().width > 0));
  }],

  ["Edit opens the form filled in, saves aliases and titles, and shows them again, at 390 px with nothing sticking out", async ({ eq, layoutSettled, ok, page, phone, sticksOut, until, waitSome }) => {
    await phone(async () => {
      await page.evaluate(() => { location.hash = "#/services"; });
      await waitSome("#services-manager-list .svc-item", 10);
      await layoutSettled();
      const row = '#services-manager-list .svc-item:has([data-service="netflix"])';
      const edit = `${row} [data-action="service-edit"]`;
      const form = () => page.$$eval("#add-service-name, #add-service-price, #add-service-aliases, #add-service-titles", (els) => els.map((e) => e.value));
      const activeBefore = await page.$$eval("#services-manager-list .svc-item.is-active", (els) => els.length);

      await page.$eval(edit, (b) => b.click());
      await until(() => document.getElementById("add-service-edit-id").value === "netflix");
      ok("the form is open", await page.$eval(".add-service-details", (d) => d.open));
      const [name, price] = await form();
      eq("the name is filled in", name, "Netflix");
      ok(`the price is filled in (${price})`, parseFloat(price) > 0);
      eq("the button says Save changes", await page.$eval("#btn-add-service-text", (e) => e.textContent), "Save changes");

      await page.$eval("#add-service-aliases", (e) => { e.value = "Netflix, Edit Alias One, Edit Alias Two"; });
      await page.$eval("#add-service-titles", (e) => { e.value = "Edit Title One\nEdit Title Two, With Comma"; });
      eq("nothing sticks out with the form open", JSON.stringify(await sticksOut()), "[]");
      await page.$eval("#btn-add-service", (b) => b.click());
      await until(() => document.getElementById("add-service-edit-id").value === "" && !!document.querySelector("#services-manager-list .svc-item"));

      const saved = await page.evaluate(async () => (await (await fetch("/api/services")).json()).find((s) => s.id === "netflix"));
      eq("saved aliases", JSON.stringify(saved.aliases), JSON.stringify(["Netflix", "Edit Alias One", "Edit Alias Two"]));
      eq("saved titles", JSON.stringify(saved.titles), JSON.stringify(["Edit Title One", "Edit Title Two, With Comma"]));
      eq("still one Netflix (an edit does not add a row)", (await page.evaluate(async () => (await (await fetch("/api/services")).json()).filter((s) => s.id.startsWith("netflix")).length)), 1);
      eq("the active subscriptions did not change", await page.$$eval("#services-manager-list .svc-item.is-active", (els) => els.length), activeBefore);

      await page.$eval(edit, (b) => b.click());
      await until(() => document.getElementById("add-service-edit-id").value === "netflix");
      const [, , aliases, titles] = await form();
      eq("Edit shows the saved aliases", aliases, "Netflix, Edit Alias One, Edit Alias Two");
      eq("Edit shows the saved titles", titles, "Edit Title One\nEdit Title Two, With Comma");
      await page.$eval("#btn-cancel-edit-service", (b) => b.click());
      eq("Cancel empties the form", (await form())[0], "");
      await layoutSettled();
      eq("nothing sticks out at 390 px", JSON.stringify(await sticksOut()), "[]");
    });
  }],
];
