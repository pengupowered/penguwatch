// The drivers behind the page (/api/drivers): which one is the library, which fill the catalogue, schedule and
// discovery roles, and what each one offers. The page never names a service: it asks here for the label to show, for
// whether there is an account to connect or sync, for a title's link on the service, and for which features are on.
//
// Markup hooks (filled whenever the drivers load, so static text needs no service name):
//   data-tpl="Reconnect {library}"       the element's text; {library} {catalog} {catalog-first} {builtin} {interval} {connect-with} are the terms
//   data-tpl-title / -aria / -placeholder   the same for the title, aria-label and placeholder attributes
//   data-needs="<role>-driver"           on a tab's section (schedule-driver, discovery-driver): without a driver for the role the tab shows a note
//   data-show-when="remote"              shown only when the condition holds: remote (the library has an account),
//                                        local (it has none), connect, credentials, diagnose (the library driver
//                                        offers them)
import { api } from "./api.js";
import { emit, on } from "./bus.js";
import { state } from "./state.js";

const FEATURE_NOTES = {
  schedule: "No schedule driver is set (drivers: schedule in config.yaml), so there is no air schedule and no calendar feed.",
  discovery: "No discovery driver is set (drivers: discovery in config.yaml), so there is nothing to discover here.",
};

export async function loadDrivers() {
  try {
    state.drivers = await api("/api/drivers");
    applyDrivers();
    await emit("drivers-loaded", state.drivers);
  } catch (err) {
    console.error("Error fetching drivers:", err);
  }
}

const info = (name) => (state.drivers?.drivers || []).find((d) => d.name === name);
const labelsOf = (names) => (names || []).map((n) => info(n)?.label || n);

// The active library driver (null until /api/drivers has answered)
export const libraryDriver = () => (state.drivers ? info(state.drivers.library) || null : null);
export const libraryLabel = () => libraryDriver()?.label || "the library service";
// Whether an account sits behind the library (true while unknown: the account's controls stay out of the way until
// the drivers have loaded, see applyDrivers)
export const hasAccount = () => state.drivers?.remote !== false;
export const catalogLabels = () => labelsOf(state.drivers?.catalog);
export const catalogLabel = () => catalogLabels().join(", ") || "the catalogue";
export const scheduleOn = () => (state.drivers?.schedule || []).length > 0;
export const discoveryOn = () => (state.drivers?.discovery || []).length > 0;
export const libraryOffers = (extra) => (libraryDriver()?.extras || []).includes(extra);

// The links to a title's page on each driver that has one and knows the title: [{ label, url }]
export function titleLinks(show) {
  const out = [];
  for (const d of state.drivers?.drivers || []) {
    const id = (show.ids || {})[d.key_provider];
    const template = (d.title_urls || {})[show.media_type === "movie" ? "movie" : show.media_type === "anime" ? "anime" : "show"];
    if (id && template) out.push({ label: d.label, url: template.replace("{id}", encodeURIComponent(id)) });
  }
  return out;
}

// The credits the running drivers' terms ask for wherever their data is shown: [{ driver, text, url }]
export function attributions() {
  return (state.drivers?.drivers || []).filter((d) => d.running && d.attribution).map((d) => ({ driver: d.name, ...d.attribution }));
}

// The credits for the drivers whose own id the title has (what the details dialog shows)
export function attributionsFor(show) {
  const have = new Set((state.drivers?.drivers || []).filter((d) => (show.ids || {})[d.key_provider]).map((d) => d.name));
  return attributions().filter((a) => have.has(a.driver));
}

// A credit as a link back to the service (new tab, no opener)
export function creditLink(a) {
  const link = document.createElement("a");
  link.href = a.url;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = a.text;
  return link;
}

// " (syncs to X)" for a label that says where a change goes; nothing when the library has no account
export const syncsTo = () => (hasAccount() ? ` (syncs to ${libraryLabel()})` : "");
export const syncingTo = () => (hasAccount() ? ` (syncing to ${libraryLabel()})` : "");

// Whether a title is known to the library driver's service (its id is there), i.e. changes to it are sent
export const syncsToLibrary = (show) => hasAccount() && Boolean((show.ids || {})[libraryDriver()?.key_provider]);

// The names of the built-in rating sources (those of every driver that ships: the Ratings tab lists them all)
export function builtinRatingNames() {
  return [...new Set((state.drivers?.drivers || []).flatMap((d) => d.rating_sources.map((r) => r.label)))];
}

// Every identifier column the drivers with an account or a catalogue know titles by (a rating CSV may name them)
export const keyProviders = () => [...new Set((state.drivers?.drivers || []).filter((d) => d.remote !== false).map((d) => d.key_provider))];

function terms() {
  const catalogs = catalogLabels();
  const names = builtinRatingNames();
  const builtin = names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}` : names[0] || "None";
  return {
    library: libraryLabel(), catalog: catalogLabel(), "catalog-first": catalogs[0] || "the catalogue", builtin,
    interval: String(state.drivers?.pull_interval_minutes ?? 30),
    "connect-with": libraryDriver()?.connect_kind === "device_code" ? " with Device PIN" : "",
  };
}
const fill = (text) => text.replace(/\{([\w-]+)\}/g, (_, key) => terms()[key] ?? "");

const CONDITIONS = {
  remote: () => hasAccount(),
  local: () => !hasAccount(),
  connect: () => hasAccount() && Boolean(libraryDriver()?.connect_kind),
  credentials: () => hasAccount() && libraryOffers("credentials"),
  diagnose: () => hasAccount() && libraryOffers("diagnose"),
};

// Fills the templated text and shows or hides what depends on the drivers, in the whole page or one inserted piece
export function applyDrivers(root = document) {
  if (!state.drivers) return;
  const t = terms();
  root.querySelectorAll("[data-tpl]").forEach((el) => { el.textContent = fill(el.dataset.tpl); });
  for (const attr of ["title", "aria", "placeholder"]) {
    root.querySelectorAll(`[data-tpl-${attr}]`).forEach((el) => {
      el.setAttribute(attr === "aria" ? "aria-label" : attr, fill(el.dataset[`tpl${attr[0].toUpperCase()}${attr.slice(1)}`]));
    });
  }
  root.querySelectorAll("[data-show-when]").forEach((el) => {
    el.hidden = !CONDITIONS[el.dataset.showWhen]?.();
  });
  if (root === document) {
    renderCredits();
    renderAttributions();
    // A tab declares the role it needs (data-needs="schedule-driver" on its section); without a driver it shows a note
    for (const pane of document.querySelectorAll(".tab-pane[data-needs]")) {
      const role = pane.dataset.needs.replace(/-driver$/, "");
      const off = !(state.drivers[role] || []).length;
      pane.classList.toggle("feature-off", off);
      let note = pane.querySelector(":scope > .feature-off-note");
      if (off && !note) {
        note = document.createElement("div");
        note.className = "empty feature-off-note";
        note.innerHTML = `<svg class="i" aria-hidden="true"><use href="#i-info"/></svg><span></span>`;
        note.querySelector("span").textContent = FEATURE_NOTES[role];
        pane.prepend(note);
      } else if (!off && note) {
        note.remove();
      }
    }
  }
  return t;
}

// "Data from <service>" for each driver that runs in a data role and has a home page
function renderCredits() {
  const host = document.getElementById("credits-text");
  if (!host) return;
  const roles = [...new Set([...(state.drivers.catalog || []), ...(state.drivers.schedule || []), ...(state.drivers.discovery || [])])];
  const credited = roles.map(info).filter((d) => d && d.home_url);
  host.replaceChildren();
  if (!credited.length) return;
  host.append("Show, movie and anime data from ");
  credited.forEach((d, i) => {
    if (i) host.append(i === credited.length - 1 ? " and " : ", ");
    const a = document.createElement("a");
    a.href = d.home_url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = d.label;
    host.append(a);
  });
  host.append(".");
}

// Until the drivers have answered, what depends on them stays hidden rather than flashing the wrong thing
on("drivers-loaded", () => applyDrivers());

// The driver credits, in the footer of every tab and in the Services & Sync tab's card; nothing when no running driver
// asks for one
function renderAttributions() {
  const credits = attributions();
  const footer = document.getElementById("app-credits");
  if (footer) {
    footer.replaceChildren();
    credits.forEach((a, i) => {
      if (i) footer.append(" · ");
      footer.append(creditLink(a));
    });
    footer.hidden = !credits.length;
  }
  const card = document.getElementById("attribution-card");
  const list = document.getElementById("attribution-list");
  if (card && list) {
    list.replaceChildren(...credits.map((a) => {
      const row = document.createElement("li");
      row.append(creditLink(a));
      return row;
    }));
    card.hidden = !credits.length;
  }
}
