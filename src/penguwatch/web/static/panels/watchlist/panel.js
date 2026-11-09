// Library tab: filters, incremental card rendering (first PAGE_SIZE, more on scroll) and per-card patching.
import { api, showLoadError, showLoading } from "../../js/api.js";
import { on } from "../../js/bus.js";
import { registerLoader } from "../../js/data.js";
import { registerAction } from "../../js/dispatch.js";
import { catalogLabel } from "../../js/drivers.js";
import { state } from "../../js/state.js";
import { activateTab, onTabActivated } from "../../js/tabs.js";
import { esc, icon } from "../../js/util.js";
import { createCard, fillCard } from "./cards.js";
import { setupTidy } from "./tidy.js";

const TAB = "watchlist-tab";
const PAGE_SIZE = 60;
let shown = [];          // the shows that pass every filter, in display order
let rendered = 0;        // how many of `shown` have a card in the DOM
let observer = null;     // watches the "show more" sentinel at the end of the grid
let fetchSeq = 0;        // drops the answer of a fetch that a newer one has overtaken

// The Library's filters (the controls above the grid); the cards read state.shows
const filters = {
  status: "watching",
  activeOnly: false,
  household: "",
  readyToBinge: false,
  serviceId: "",
  mediaType: "",
  sortBy: "priority",
  minSource: "",      // a rating source id, with minValue: only titles rated at least that
  minValue: "",
  search: "",
};

const gridEl = () => document.getElementById("shows-container");

// `keep` re-renders as many cards as were showing (used after a change); otherwise the list restarts from the top.
async function fetchShows({ keep = false } = {}) {
  const seq = ++fetchSeq;
  showLoading(gridEl(), "Loading your library…");
  try {
    const params = new URLSearchParams({ view: "card" });
    const f = filters;
    if (f.status) params.append("status", f.status);
    if (f.activeOnly) params.append("only_active_services", "true");
    // a person: the titles they want alone (shared ones are under "shared"), as "Me only" was before 0.17
    if (f.household === "shared") params.append("shared", "true");
    else if (f.household) { params.append("member", f.household); params.append("shared", "false"); }
    if (f.serviceId) params.append("service_id", f.serviceId);
    if (f.mediaType) params.append("media_type", f.mediaType);
    if (f.sortBy) params.append("sort_by", f.sortBy);
    if (f.minSource && f.minValue !== "" && Number.isFinite(Number(f.minValue))) params.append("min_rating", `${f.minSource}:${f.minValue}`);
    if (f.search) params.append("search", f.search);
    const shows = await api(`/api/shows?${params.toString()}`);
    if (seq !== fetchSeq) return;
    state.shows = shows;
    applyFiltersAndRender(keep);
  } catch (err) {
    console.error("Error fetching shows:", err);
    showLoadError(gridEl(), err);
  }
}

function applyFiltersAndRender(keep = false) {
  const upTo = keep ? Math.max(rendered, PAGE_SIZE) : PAGE_SIZE;
  shown = state.shows.filter((s) => !filters.readyToBinge || s.is_ready_to_binge);   // a copy: edits below keep both lists in step
  renderShows(upTo);
}

function setCount() {
  const el = document.getElementById("library-count");
  if (el) el.textContent = `${shown.length} ${shown.length === 1 ? "title" : "titles"}`;
}

// --- Library ---

function emptyStateHtml() {
  const q = filters.search.trim();
  return q
    ? `<div class="empty">${icon("search")}<span><b>"${esc(q)}" isn't in your library${filters.status ? " with these filters" : ""}.</b>
          <button class="btn primary" data-call="openAddModal" data-args='${esc(JSON.stringify([q]))}'>${icon("search")}Search ${esc(catalogLabel())} for "${esc(q)}" and add it</button></span>
        </div>`
    : `<div class="empty">${icon("library")}<span><b>Nothing matches these filters</b>Try other filters, or use Add title to search ${esc(catalogLabel())}.</span></div>`;
}

function renderShows(upTo = PAGE_SIZE) {
  const container = gridEl();
  if (observer) observer.disconnect();
  container.replaceChildren();
  rendered = 0;
  setCount();
  if (!shown.length) {
    container.innerHTML = emptyStateHtml();
    return;
  }
  renderTo(upTo);
}

// Make sure `target` cards (or all of them, if fewer) are in the DOM, then put the "show more" sentinel after them.
function renderTo(target) {
  const container = gridEl();
  container.querySelector("#shows-sentinel")?.remove();
  const end = Math.min(shown.length, target);
  const frag = document.createDocumentFragment();
  for (; rendered < end; rendered++) frag.appendChild(createCard(shown[rendered]));
  container.appendChild(frag);
  if (rendered >= shown.length) return;
  const sentinel = document.createElement("div");
  sentinel.id = "shows-sentinel";
  sentinel.className = "shows-sentinel";
  sentinel.innerHTML = `<button type="button" class="btn outline" data-action="show-more">Show more (${shown.length - rendered} left)</button>`;
  container.appendChild(sentinel);
  if (typeof IntersectionObserver === "undefined") return;   // the button is the fallback
  observer ||= new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) renderTo(rendered + PAGE_SIZE);
  }, { rootMargin: "800px 0px" });
  observer.disconnect();
  observer.observe(sentinel);
}

// Would the server still return this show for the active filters? (Search is not re-checked: no editable field changes it.)
function matchesFilters(show) {
  const f = filters;
  if (f.status && show.status !== f.status) return false;
  const who = show.household || [];
  if (f.household === "shared" && who.length < 2) return false;
  if (f.household && f.household !== "shared" && !(who.length === 1 && who[0] === f.household)) return false;
  if (f.mediaType && (show.media_type || "show") !== f.mediaType) return false;
  if (f.readyToBinge && !show.is_ready_to_binge) return false;
  if (f.serviceId && !f.activeOnly) {
    const none = f.serviceId === "unassigned" || f.serviceId === "none";
    if (none ? show.service_id : show.service_id !== f.serviceId) return false;
  }
  return true;
}

// Patch one card from the Show an endpoint returned; drop it if it no longer matches the filters.
function applyShowUpdate(updated) {
  const idx = state.shows.findIndex((s) => s.id === updated.id);
  if (idx === -1) return;
  const before = state.shows[idx];
  if (filters.activeOnly && before.service_id !== updated.service_id) {
    fetchShows({ keep: true });     // "on an active service" depends on bundles; let the server decide
    return;
  }
  if (!matchesFilters(updated)) return removeShow(updated.id);
  state.shows[idx] = updated;
  const pos = shown.indexOf(before);
  if (pos === -1) return;
  shown[pos] = updated;
  const card = gridEl().querySelector(`[data-show-id="${updated.id}"]`);
  if (card) fillCard(card, updated);
}

// Remove a show from the list and its card from the grid (deleted, or no longer matching the filters).
function removeShow(showId) {
  const idx = state.shows.findIndex((s) => s.id === showId);
  if (idx === -1) return;
  const [gone] = state.shows.splice(idx, 1);
  const pos = shown.indexOf(gone);
  if (pos === -1) return;
  shown.splice(pos, 1);
  if (pos < rendered) rendered--;
  gridEl().querySelector(`[data-show-id="${showId}"]`)?.remove();
  setCount();
  if (!shown.length) renderShows();
  else renderTo(Math.max(rendered, PAGE_SIZE));   // refresh the "N left" count and keep a full page showing
}

// --- Library filters ---

function setStatusTab(status) {
  document.querySelectorAll("#watchlist-tab .filter-tab").forEach((t) => {
    const on = t.getAttribute("data-status") === status;
    t.classList.toggle("active", on);
    t.setAttribute("aria-pressed", String(on));
  });
  filters.status = status;
}

function setupFilterListeners() {
  document.querySelectorAll("#watchlist-tab .filter-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      setStatusTab(tab.getAttribute("data-status"));
      fetchShows();
    });
  });
  const bind = (id, prop, evt = "change", getter = (e) => e.target.value) => {
    const el = document.getElementById(id);
    if (el)
      el.addEventListener(evt, (e) => {
        filters[prop] = getter(e);
        prop === "readyToBinge" ? applyFiltersAndRender() : fetchShows();
      });
  };
  bind("filter-active-services", "activeOnly", "change", (e) => e.target.checked);
  bind("filter-household-select", "household");
  bind("filter-ready-binge", "readyToBinge", "change", (e) => e.target.checked);
  bind("filter-service-select", "serviceId");
  bind("filter-media-select", "mediaType");
  bind("sort-by-select", "sortBy");
  bind("filter-minrating-source", "minSource");
  bind("filter-minrating-value", "minValue", "change");
  document.getElementById("filter-minrating-source")?.addEventListener("change", (e) => {
    const value = document.getElementById("filter-minrating-value");
    value.disabled = !e.target.value;
    if (!e.target.value) { value.value = ""; filters.minValue = ""; }
  });

  let searchTimeout = null;
  document.getElementById("watchlist-search").addEventListener("input", (e) => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => {
      filters.search = e.target.value;
      fetchShows();
    }, 300);
  });
}

// The sort and "at least" pickers list the rating sources; refilled when the list changes. Option text is set as text.
function fillRatingControls(sources) {
  const option = (value, text) => {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = text;
    return o;
  };
  const sort = document.getElementById("sort-by-select");
  if (sort) {
    sort.querySelector("optgroup[data-ratings]")?.remove();
    sort.querySelectorAll("option[data-builtin]").forEach((o) => o.remove());
    // the built-in sources (the drivers'), right after "Priority & Rating"
    sources.filter((s) => s.builtin).reverse().forEach((s) => {
      const o = option(s.id, `Highest ${s.label} Rating`);
      o.dataset.builtin = "1";
      sort.options[0].after(o);
    });
    const mine = sources.filter((s) => !s.builtin);
    if (mine.length) {
      const group = document.createElement("optgroup");
      group.label = "Your rating sources";
      group.dataset.ratings = "1";
      mine.forEach((s) => group.appendChild(option(`rating:${s.id}`, `Highest ${s.name}`)));
      sort.appendChild(group);
    }
    if (filters.sortBy.startsWith("rating:") && !sources.some((s) => `rating:${s.id}` === filters.sortBy)) {
      filters.sortBy = "priority";
      sort.value = "priority";
      fetchShows();
    } else sort.value = filters.sortBy;
  }
  const min = document.getElementById("filter-minrating-source");
  if (min) {
    min.replaceChildren(option("", "Any rating"), ...sources.map((s) => option(s.id, `${s.name} at least`)));
    if (filters.minSource && !sources.some((s) => s.id === filters.minSource)) {
      filters.minSource = "";
      filters.minValue = "";
      document.getElementById("filter-minrating-value").value = "";
      document.getElementById("filter-minrating-value").disabled = true;
      fetchShows();
    }
    min.value = filters.minSource;
  }
}

// Poster cards vs. a compact list. Remembered per browser; "#/library?view=compact" forces it.
function setupViewToggle() {
  const btn = document.getElementById("btn-view-toggle");
  const grid = gridEl();
  if (!btn || !grid) return;
  const apply = (compact) => {
    grid.classList.toggle("compact", compact);
    btn.setAttribute("aria-pressed", String(compact));
    btn.innerHTML = compact ? `${icon("heat")}<span>Cards</span>` : `${icon("list")}<span>Compact</span>`;
  };
  let compact = false;
  try { compact = localStorage.getItem("wt-view") === "compact"; } catch { /* private mode */ }
  const fromUrl = new URLSearchParams(location.hash.split("?")[1] || "").get("view");
  if (fromUrl) compact = fromUrl === "compact";
  apply(compact);
  btn.addEventListener("click", () => {
    compact = !compact;
    apply(compact);
    try { localStorage.setItem("wt-view", compact ? "compact" : "cards"); } catch { /* ignore */ }
  });
}

// The services filter lists every service; refilled whenever the list changes
function fillServiceFilter(services) {
  const select = document.getElementById("filter-service-select");
  if (!select) return;
  const currentVal = select.value;
  select.innerHTML = '<option value="">All Services / Networks</option><option value="unassigned">Unassigned / Other</option>';
  services.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = s.is_active ? `${s.name} (active)` : s.name;
    select.appendChild(opt);
  });
  select.value = currentVal;
}

// Another tab asks for one service's titles (the Optimizer's "View"): every other filter is reset.
function showServiceTitles(serviceId) {
  activateTab(TAB);
  const select = document.getElementById("filter-service-select");
  if (select) select.value = serviceId;
  filters.serviceId = serviceId;
  const reset = (id, prop, val) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === "checkbox") el.checked = val;
    else el.value = val;
    filters[prop] = val;
  };
  reset("filter-active-services", "activeOnly", false);
  reset("filter-household-select", "household", "");
  reset("filter-ready-binge", "readyToBinge", false);
  reset("filter-media-select", "mediaType", "");
  reset("watchlist-search", "search", "");
  reset("filter-minrating-source", "minSource", "");
  reset("filter-minrating-value", "minValue", "");
  document.getElementById("filter-minrating-value").disabled = true;
  setStatusTab("");
  fetchShows();
}

registerAction("show-more", () => renderTo(rendered + PAGE_SIZE));
onTabActivated(TAB, () => fetchShows());
registerLoader(fetchShows);
on("services-loaded", fillServiceFilter);
on("rating-sources-loaded", fillRatingControls);
on("show-updated", applyShowUpdate);
on("show-removed", removeShow);
on("subscriptions-changed", () => fetchShows());
on("titles-changed", () => (state.activeTab === TAB ? fetchShows({ keep: true }) : undefined));
on("show-service-titles", showServiceTitles);

export default {
  id: "watchlist",
  mount() {
    setupViewToggle();
    setupFilterListeners();
    setupTidy();
  },
};
