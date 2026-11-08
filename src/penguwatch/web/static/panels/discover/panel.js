// Discover tab: trending and recommendations.
import { api } from "../../js/api.js";
import { on } from "../../js/bus.js";
import { refreshAfterChange } from "../../js/data.js";
import { register } from "../../js/dispatch.js";
import { discoveryOn, hasAccount, libraryLabel } from "../../js/drivers.js";
import { state } from "../../js/state.js";
import { onTabActivated } from "../../js/tabs.js";
import { PLACEHOLDER_POSTER, STATUS_LABELS, esc, householdShared, icon, mediaIcon, showToast } from "../../js/util.js";

const discover = { mode: "trending", media: "tv", interval: "week", hideLibrary: true, items: [], byIndex: [] };

// --- Discover ---

function setupDiscover() {
  document.querySelectorAll("#discover-mode .filter-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll("#discover-mode .filter-tab").forEach((t) => {
        t.classList.toggle("active", t === tab);
        t.setAttribute("aria-pressed", String(t === tab));
      });
      discover.mode = tab.getAttribute("data-mode");
      document.getElementById("discover-trending-controls").hidden = discover.mode !== "trending";
      loadDiscover();
    });
  });
  document.getElementById("discover-media").addEventListener("change", (e) => {
    discover.media = e.target.value;
    loadDiscover();
  });
  document.getElementById("discover-interval").addEventListener("change", (e) => {
    discover.interval = e.target.value;
    loadDiscover();
  });
  document.getElementById("discover-hide-library").addEventListener("change", (e) => {
    discover.hideLibrary = e.target.checked;
    renderDiscover(discover.items || []);
  });
}

async function loadDiscover() {
  const container = document.getElementById("discover-container");
  const note = document.getElementById("discover-note");
  if (!discoveryOn()) {
    // No discovery driver at all: the tab shows a note instead (js/drivers.js). A driver that is not signed in: say so
    // here, without asking the server (which would answer 400).
    if (state.drivers?.discovery?.length) {
      container.innerHTML = `<div class="empty">${icon("compass")}<span>No discovery source is set up.</span></div>`;
      note.hidden = true;
    }
    return;
  }
  container.innerHTML = '<div class="empty loading-state"><span class="spinner" aria-hidden="true"></span><span>Loading…</span></div>';
  note.hidden = true;
  try {
    let items;
    if (discover.mode === "trending") {
      items = await api(`/api/discover/trending?media=${discover.media}&interval=${discover.interval}`);
    } else {
      const data = await api("/api/discover/recommendations");
      if (!data.enabled) {
        container.innerHTML = "";
        note.innerHTML = `${icon("bulb")}<span>${hasAccount() ? `Connect ${esc(libraryLabel())} (Services &amp; Sync)` : "Rate some titles"} to get personal recommendations.</span>`;
        note.hidden = false;
        return;
      }
      items = data.items;
      if (data.seeds && data.seeds.length) {
        note.innerHTML = `${icon("sparkle")}<span>Based on: ${data.seeds.slice(0, 6).map(esc).join(", ")}${data.seeds.length > 6 ? "…" : ""}</span>`;
        note.hidden = false;
      }
    }
    discover.items = items;
    renderDiscover(items);
  } catch (err) {
    container.innerHTML = `<div class="empty st st-crit">${icon("warn")}<span>${esc(err.message)}</span></div>`;
  }
}

function renderDiscover(items) {
  const container = document.getElementById("discover-container");
  const visible = discover.hideLibrary ? items.filter((i) => !i.in_library) : items;
  if (!visible.length) {
    container.innerHTML = items.length
      ? `<div class="empty">${icon("ok")}<span>Nothing new here. You've already got all of these.</span></div>`
      : `<div class="empty">${icon("compass")}<span>Couldn't load this list right now. Try again in a minute.</span></div>`;
    return;
  }
  discover.byIndex = visible;
  container.innerHTML = visible
    .map((it, idx) => {
      const rating = it.imdb_rating
        ? `<span class="rating" title="IMDb rating"><b>IMDb</b> ${Number(it.imdb_rating).toFixed(1)}</span>`
        : "";
      const svc = it.service_name ? `<span class="service-tag ${it.service_active ? "svc-on" : ""}">${esc(it.service_name)}</span>` : "";
      const because = it.because && it.because.length ? `<div class="discover-because">Because you liked ${esc(it.because.slice(0, 2).join(" & "))}</div>` : "";
      const actions = it.in_library
        ? `<button class="btn sm" data-call="openShow" data-args='[${it.library_id}]'>${icon("library")}In library · ${esc(STATUS_LABELS[it.library_status] || it.library_status)}</button>`
        : `${householdShared ? `<button class="btn sm outline" data-call="addDiscovered" data-args='[${idx}, "both"]'>${icon("users")}We both want it</button>` : ""}
           <button class="btn sm outline" data-call="addDiscovered" data-args='[${idx}, "person1"]'>${icon("plus")}Add</button>`;
      return `
        <div class="discover-card">
          <img class="poster discover-poster" src="${esc(it.poster || PLACEHOLDER_POSTER)}" alt="" loading="lazy" data-fallback="poster">
          <div class="discover-info">
            <div class="discover-title">${mediaIcon(it.media_type)} ${esc(it.title)} <span class="show-year">${it.year ? `(${it.year})` : ""}</span></div>
            <div class="discover-badges">${rating} ${svc}</div>
            ${because}
            <div class="discover-overview">${esc((it.overview || "").slice(0, 220))}${(it.overview || "").length > 220 ? "…" : ""}</div>
            <div class="discover-actions">${actions}</div>
          </div>
        </div>`;
    })
    .join("");
}

async function addDiscovered(idx, household) {
  const it = discover.byIndex[idx];
  if (!it) return;
  try {
    const created = await api("/api/discover/add", {
      method: "POST",
      body: {
        ids: it.ids || {},
        media_type: it.media_type,
        title: it.title,
        year: it.year,
        poster: it.poster,
        overview: it.overview,
        household_tag: household,
      },
    });
    it.in_library = true;
    it.library_status = created.status;
    showToast(`Added ${created.title} to Plan to Watch`);
    renderDiscover(discover.items);
    refreshAfterChange();
  } catch (err) {
    showToast(err.message, true);
  }
}

onTabActivated("discover-tab", () => loadDiscover());
// the login state arrives after the page loads (and changes when the account is connected or disconnected)
on("library-status", () => { if (state.activeTab === "discover-tab") loadDiscover(); });
register({ addDiscovered });

export default { id: "discover", mount: setupDiscover };
