// Whole-page data loading and the lazy refresh that follows changes; shared by all tabs.
// The data several tabs need (the services, the header's stats, the drivers, the library status) is loaded here; each tab loads its
// own through registerLoader() and listens on the bus (bus.js) for what changed.
import { api } from "./api.js";
import { emit } from "./bus.js";
import { loadDrivers } from "./drivers.js";
import { checkLibraryStatus } from "./library-status.js";
import { state } from "./state.js";

const SOON_MS = 150;
const LATER_MS = 1500;
const loaders = [];
let soonTimer = null;
let laterTimer = null;

// A tab's or dialog's first load, run by loadAllData() (and again after a sync).
export function registerLoader(fn) {
  loaders.push(fn);
}

// The streaming services (state.services): the Library filter, the cards' service picker, the Services tab and the
// details dialog all read them. "services-loaded" tells them the list changed.
export async function loadServices() {
  try {
    state.services = await api("/api/services");
    await emit("services-loaded", state.services);
  } catch (err) {
    console.error("Error fetching services:", err);
  }
}

// The rating sources (state.ratingSources): built-in and custom. The Library's sort, filter and card badges and the
// details dialog read them; the Ratings tab changes them. "rating-sources-loaded" tells them the list changed.
export async function loadRatingSources() {
  try {
    state.ratingSources = await api("/api/rating-sources");
    await emit("rating-sources-loaded", state.ratingSources);
  } catch (err) {
    console.error("Error fetching rating sources:", err);
  }
}

// Library and spending totals (header, Up Next strip, Optimizer figures): "stats".
export async function loadStats() {
  try {
    state.stats = await api("/api/stats");
    await emit("stats", state.stats);
  } catch (err) {
    console.error("Error fetching stats:", err);
  }
}

export async function loadAllData() {
  await Promise.all([loadServices(), loadDrivers()]);
  await Promise.all([loadRatingSources(), loadStats(), checkLibraryStatus(), ...loaders.map((fn) => fn())]);
}

// A subscription was switched on or off: whatever depends on which services are active refreshes
// ("subscriptions-changed": the plan, the Library, Up Next) along with the stats.
export async function subscriptionsChanged() {
  await loadServices();
  await Promise.all([loadStats(), emit("subscriptions-changed")]);
}

// A price, renewal or note changed, or a service was added: the plan and the stats.
export async function pricesChanged() {
  await loadServices();
  await Promise.all([loadStats(), emit("prices-changed")]);
}

// After a change, only what is on screen is refreshed, and not on every click: "changed-soon" right away (a short
// debounce) for the open tab's own view, "changed-later" 1.5 s after the last change for the header figures (the
// plan, the pending-sync count).
export function scheduleSideRefresh() {
  clearTimeout(soonTimer);
  soonTimer = setTimeout(() => emit("changed-soon"), SOON_MS);
  clearTimeout(laterTimer);
  laterTimer = setTimeout(() => { emit("changed-later"); checkLibraryStatus(true); }, LATER_MS);
}

// One show changed: whoever shows it patches it from the Show the endpoint returned, then the rest refreshes lazily.
export function afterShowChange(show) {
  emit("show-updated", show);
  scheduleSideRefresh();
}

// Many shows may have changed (add, review, tidy): the open Library re-fetches, the rest refreshes lazily.
export async function refreshAfterChange() {
  await emit("titles-changed");
  scheduleSideRefresh();
}
