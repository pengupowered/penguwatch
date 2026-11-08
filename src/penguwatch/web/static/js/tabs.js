// Tab navigation: hash routes, tab activation and the keyboard shortcuts. The tabs are whatever the server found
// (penguwatch.ui): each tab button carries its section (data-tab), its #/<route> (data-route) and its "g <key>"
// shortcut (data-key), so nothing here names a tab.
import { state } from "./state.js";
import { showToast } from "./util.js";

// Per-tab refresh callbacks (several modules may refresh the same tab), registered by the modules that own the data
const tabCallbacks = {};

export function onTabActivated(tabId, fn) {
  (tabCallbacks[tabId] ||= []).push(fn);
}

const buttons = () => [...document.querySelectorAll(".nav-btn[data-tab]")];

// "#/<route>" addresses so tabs can be bookmarked, shared and reached with the back button
export function tabRoutes() {
  return Object.fromEntries(buttons().map((b) => [b.dataset.route, b.dataset.tab]));
}

function routeOf(tabId) {
  return buttons().find((b) => b.dataset.tab === tabId)?.dataset.route;
}

export function activateTab(targetId, { quiet = false } = {}) {
  buttons().forEach((b) => {
    const on = b.dataset.tab === targetId;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
    b.tabIndex = on ? 0 : -1;
  });
  document.querySelectorAll(".tab-pane").forEach((p) => p.classList.toggle("active", p.id === targetId));
  state.activeTab = targetId;
  if (!quiet) (tabCallbacks[targetId] || []).forEach((fn) => fn());
}

// Opens a tab the way its button does: through the hash (a history entry), or directly when it is already there.
export function goToTab(targetId) {
  const route = routeOf(targetId);
  if (route && tabFromHash() !== targetId) location.hash = "#/" + route;
  else activateTab(targetId);
}

export function tabFromHash() {
  const route = (location.hash.replace(/^#\/?/, "") || "").split("?")[0];
  return tabRoutes()[route] || null;
}

// The desktop tab strip shows full labels while they fit and the short ones (Plan, Pick, Schedule...) when they do not,
// decided by measuring: the strip is measured with the full labels, and if it overflows it goes compact (`tabs-compact`).
// No width is hard-coded, because label widths depend on the theme and the font. The class change happens before the next
// paint, so nothing flickers; the strip's own width does not change with it, so the observer does not loop. If even the
// short labels overflow too (about 1000 px and narrower, above the phone layout), only the icons show (`tabs-icons`; each tab
// keeps its name as aria-label and tooltip); and if even those do not fit, the strip scrolls sideways under a fade (`tabs-scroll`).
export function setupTabFit() {
  const bar = document.getElementById("tabs");
  if (!bar) return;
  bar.querySelectorAll(".nav-btn[aria-label]").forEach((b) => { b.title = b.getAttribute("aria-label"); });
  const phone = window.matchMedia("(max-width: 720px)");
  let queued = false;
  const overflows = () => bar.scrollWidth > bar.clientWidth + 1;
  const fit = () => {
    queued = false;
    bar.classList.remove("tabs-compact", "tabs-icons", "tabs-scroll");
    if (phone.matches || !overflows()) return;
    bar.classList.add("tabs-compact");
    if (!overflows()) return;
    bar.classList.add("tabs-icons");
    if (overflows()) bar.classList.add("tabs-scroll");
  };
  const soon = () => { if (!queued) { queued = true; requestAnimationFrame(fit); } };
  new ResizeObserver(soon).observe(bar);
  document.addEventListener("themechange", soon);
  phone.addEventListener("change", soon);
  document.fonts?.ready.then(soon);
  fit();
}

export function setupNavigation() {
  setupTabFit();
  const all = buttons();
  all.forEach((btn) => btn.addEventListener("click", () => goToTab(btn.dataset.tab)));
  // Arrow keys move between the tabs of the tab strip (one tab stop: the selected tab)
  document.getElementById("tabs")?.addEventListener("keydown", (ev) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(ev.key) || !ev.target.matches(".nav-btn")) return;
    const shown = all.filter((b) => b.offsetParent !== null);
    const at = shown.indexOf(ev.target);
    const next = ev.key === "Home" ? 0 : ev.key === "End" ? shown.length - 1 : (at + (ev.key === "ArrowRight" ? 1 : -1) + shown.length) % shown.length;
    ev.preventDefault();
    shown[next]?.focus();
  });
  window.addEventListener("hashchange", () => {
    const t = tabFromHash();
    if (t && t !== state.activeTab) activateTab(t);
  });
  if (tabFromHash()) activateTab(tabFromHash());
  else if (all.length) activateTab(all[0].dataset.tab, { quiet: true }); // its data comes with the first load

  // Keyboard: "g" then a letter jumps to a tab, "/" focuses the first search box of the open tab, "?" lists the keys
  const go = Object.fromEntries(all.filter((b) => b.dataset.key).map((b) => [b.dataset.key, b.dataset.route]));
  let goUntil = 0;
  document.addEventListener("keydown", (ev) => {
    const t = ev.target;
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (t && (t.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName))) return;
    if (document.querySelector(".modal.active")) return;
    if (Date.now() < goUntil && go[ev.key]) {
      goUntil = 0;
      location.hash = "#/" + go[ev.key];
    } else if (ev.key === "g") {
      goUntil = Date.now() + 1200;
    } else if (ev.key === "/") {
      const search = document.querySelector("[data-search-shortcut]");
      if (!search) return;
      ev.preventDefault();
      const pane = search.closest(".tab-pane");
      if (pane && pane.id !== state.activeTab) goToTab(pane.id);
      setTimeout(() => search.focus(), 50);
    } else if (ev.key === "?") {
      showToast(`g then ${Object.keys(go).join("/")} jumps to a tab · / filters the library`);
    }
  });
}
