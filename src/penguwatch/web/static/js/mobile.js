// Mobile chrome: the "More" sheet behind the bottom bar. The bar shows the tabs marked for it (data-bar, from each
// tab's panel.json); the sheet lists the others. Navigation itself stays in tabs.js: sheet items click the original
// (hidden) .nav-btn.
import { state } from "./state.js";

function focusables(sheet) {
  return [...sheet.querySelectorAll("button:not([disabled])")];
}

function setupMoreSheet() {
  const more = document.getElementById("nav-more");
  const sheet = document.getElementById("more-sheet");
  const backdrop = document.getElementById("more-backdrop");
  const list = document.getElementById("more-list");
  if (!more || !sheet || !backdrop || !list) return;

  const hidden = [...document.querySelectorAll(".nav-btn[data-tab]")].filter((b) => !("bar" in b.dataset));
  list.replaceChildren();
  for (const btn of hidden) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "more-item";
    item.dataset.moreTab = btn.dataset.tab;
    const ic = btn.querySelector("svg");
    if (ic) item.appendChild(ic.cloneNode(true));
    const label = document.createElement("span");
    label.textContent = btn.querySelector(".nav-full")?.textContent.trim() || btn.textContent.trim();
    item.appendChild(label);
    list.appendChild(item);
  }

  let opener = more;
  const isOpen = () => !sheet.hidden;
  const open = () => {
    opener = document.activeElement instanceof HTMLElement ? document.activeElement : more;
    sheet.hidden = false;
    backdrop.hidden = false;
    more.setAttribute("aria-expanded", "true");
    (list.querySelector(".more-item.active") || focusables(sheet)[0])?.focus();
  };
  const close = () => {
    if (!isOpen()) return;
    sheet.hidden = true;
    backdrop.hidden = true;
    more.setAttribute("aria-expanded", "false");
    opener.focus();
  };

  more.addEventListener("click", () => (isOpen() ? close() : open()));
  backdrop.addEventListener("click", close);
  list.addEventListener("click", (ev) => {
    const item = ev.target.closest(".more-item");
    if (!item) return;
    document.querySelector(`.nav-btn[data-tab="${item.dataset.moreTab}"]`)?.click();
    sheet.hidden = true;     // activation closes the sheet; focus moves to the new tab content area
    backdrop.hidden = true;
    more.setAttribute("aria-expanded", "false");
    more.focus();
  });

  // Capture phase and stopPropagation: while the sheet is open no page shortcut (g u, /, ?) may fire.
  document.addEventListener("keydown", (ev) => {
    if (!isOpen()) return;
    if (ev.key === "Escape") {
      ev.preventDefault();
      close();
    } else if (ev.key === "Tab") {
      const items = focusables(sheet);
      const first = items[0];
      const last = items[items.length - 1];
      if (!sheet.contains(document.activeElement)) { ev.preventDefault(); first?.focus(); }
      else if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
      else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
    }
    ev.stopPropagation();
  }, true);

  // Reflect a hidden tab being active on the More button (tabs.js toggles .active on every .nav-btn)
  const reflect = () => {
    const inSheet = hidden.some((b) => b.dataset.tab === state.activeTab);
    more.classList.toggle("active", inSheet);
    more.setAttribute("aria-current", inSheet ? "page" : "false");
    list.querySelectorAll(".more-item").forEach((i) => i.classList.toggle("active", i.dataset.moreTab === state.activeTab));
  };
  const nav = document.getElementById("tabs");
  if (nav) new MutationObserver(reflect).observe(nav, { subtree: true, attributes: true, attributeFilter: ["class"] });
  reflect();
}

export function setupMobile() {
  setupMoreSheet();
}
