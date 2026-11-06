// Entry point of the ES-module app. Starts the shell (theme, header, tabs, dialogs), then loads every tab's and
// shared dialog's module the server found (penguwatch.ui lists them in <body data-modules>) and mounts them; nothing
// here names a tab. Importing a module registers its allow-listed handlers (dispatch.js), its tab-refresh callbacks
// (tabs.js), its loaders (data.js) and its bus listeners (bus.js).
import { initChrome } from "./chrome.js";
import { loadAllData } from "./data.js";
import { setupDispatch } from "./dispatch.js";
import { setupHeader } from "./header.js";
import { setupMobile } from "./mobile.js";
import { setupModals } from "./modal.js";
import { checkLibraryStatus } from "./library-status.js";
import { setupNavigation } from "./tabs.js";
import "./showchanges.js";

async function loadModules() {
  let listed = { dialogs: [], panels: [] };
  try {
    listed = JSON.parse(document.body.dataset.modules || "{}");
  } catch (err) {
    console.error("The page lists no modules:", err);
  }
  const paths = [...(listed.dialogs || []), ...(listed.panels || [])];
  // relative to this file (static/js/), so every module inherits the page's /static/v/<hash>/ prefix. A module that
  // fails to load costs its own tab only.
  const results = await Promise.allSettled(paths.map((path) => import(`../${path}`)));
  results.forEach((r, i) => { if (r.status === "rejected") console.error(`${paths[i]} did not load:`, r.reason); });
  return results.filter((r) => r.status === "fulfilled").map((r) => r.value);
}

document.addEventListener("DOMContentLoaded", async () => {
  setupDispatch();
  initChrome();
  setupModals();
  setupHeader();
  const modules = await loadModules();
  for (const module of modules) module.default?.mount?.();
  setupNavigation();
  setupMobile();
  loadAllData();
  // Keep the pending-sync count fresh when the page is left open on a TV or tablet
  setInterval(() => {
    if (document.visibilityState === "visible") checkLibraryStatus(true);
  }, 60000);
});

// Installable app: the worker caches only the static shell (see /sw.js); failures are harmless.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
  });
}
