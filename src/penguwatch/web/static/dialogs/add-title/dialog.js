// Add show / movie dialog: search the catalogue and track a result, or add a title by hand. Opened from the header and the
// Library with data-call="openAddModal" (data-args '["<query>"]' searches right away).
import { api } from "../../js/api.js";
import { refreshAfterChange } from "../../js/data.js";
import { register } from "../../js/dispatch.js";
import { catalogLabel, hasAccount, libraryLabel, syncsToLibrary } from "../../js/drivers.js";
import { closeModal, openModal } from "../../js/modal.js";
import { displayTitle, esc, icon, mediaIcon, showToast, subtitleHtml } from "../../js/util.js";

let searchResults = [];

// --- Modals ---

function setupModalListeners() {
  document.getElementById("manual-add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {
      title: document.getElementById("m-title").value.trim(),
      year: parseInt(document.getElementById("m-year").value) || null,
      network: document.getElementById("m-network").value.trim() || null,
      media_type: document.getElementById("m-media").value,
      imdb_rating: parseFloat(document.getElementById("m-rating").value) || null,
      status: document.getElementById("m-status").value,
      household_tag: document.getElementById("m-household").value,
      priority: document.getElementById("m-priority").value,
    };
    try {
      const created = await api("/api/shows", { method: "POST", body: payload });
      showToast(`Added ${displayTitle(created)}${syncsToLibrary(created) ? ` (linked to ${libraryLabel()})` : ""}!`);
      closeModal("add-show-modal");
      document.getElementById("manual-add-form").reset();
      await refreshAfterChange();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  document.getElementById("btn-catalog-search").addEventListener("click", performCatalogSearch);
  document.getElementById("catalog-search-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") performCatalogSearch();
  });
}

function openAddModal(query = "") {
  openModal("add-show-modal");
  const input = document.getElementById("catalog-search-input");
  if (query) {
    input.value = query;
    performCatalogSearch();
  } else {
    document.getElementById("catalog-search-results").innerHTML = "";
  }
  input.focus();
}

async function performCatalogSearch() {
  const query = document.getElementById("catalog-search-input").value.trim();
  if (!query) return;
  const type = document.getElementById("catalog-search-type").value;
  const resultsContainer = document.getElementById("catalog-search-results");
  resultsContainer.innerHTML = `<div class="empty loading-state"><span class="spinner" aria-hidden="true"></span><span>Searching ${esc(catalogLabel())}…</span></div>`;
  try {
    const data = await api(`/api/catalog/search?q=${encodeURIComponent(query)}&type=${type}`);
    searchResults = data;
    if (!data.length) {
      resultsContainer.innerHTML = `<div class="empty">${icon("search")}<span>Nothing found.</span></div>`;
      return;
    }
    resultsContainer.innerHTML = data
      .map((item, idx) => {
        const rating = item.ratings && item.ratings.imdb && item.ratings.imdb.rating ? `<span class="rating" title="IMDb rating"><b>IMDb</b> ${esc(item.ratings.imdb.rating)}</span>` : "";
        const serviceBadge = item.service_name && item.service_name !== "Unknown" ? `<span class="service-tag">${esc(item.service_name)}</span>` : "";
        const kind = { movie: "Movie", anime: "Anime", show: "TV" }[item.media_type] || "TV";
        const sub = [kind, item.network].filter(Boolean).join(" · ");
        return `
        <div class="search-item">
          <img class="poster search-item-thumb" src="${esc(item.poster || "")}" alt="" data-fallback="poster">
          <div class="search-item-details">
            <div class="search-item-title">${mediaIcon(item.media_type)} ${esc(displayTitle(item))} (${esc(item.year || "?")}) ${rating}</div>
            ${subtitleHtml(item)}
            <div class="search-item-sub">${esc(sub)} ${serviceBadge}</div>
          </div>
          ${
            item.in_library
              ? `<span class="badge quiet st-good">${icon("ok")}Tracked</span>`
              : `<button class="btn sm outline" data-call="addShowFromSearch" data-args='[${idx}]'>${icon("plus")}Track</button>`
          }
        </div>`;
      })
      .join("");
  } catch (err) {
    resultsContainer.innerHTML = `<div class="empty st st-warn">${icon("warn")}<span>${esc(err.message)}</span></div>`;
  }
}

async function addShowFromSearch(idx) {
  const item = searchResults[idx];
  if (!item) return;
  try {
    const payload = { title: item.title || "New title", ids: item.ids || {}, media_type: item.media_type };
    if (item.service_id) payload.service_id = item.service_id;
    if (item.network) payload.network = item.network;
    const created = await api("/api/shows", { method: "POST", body: payload });
    showToast(`Added ${displayTitle(created)}${hasAccount() && syncsToLibrary(created) ? ` (also added to your ${libraryLabel()} list)` : ""}`);
    closeModal("add-show-modal");
    await refreshAfterChange();
  } catch (err) {
    showToast(err.message, true);
  }
}

register({ addShowFromSearch, openAddModal: (query) => openAddModal(typeof query === "string" ? query.trim() : "") });

export default { id: "add-title", mount: setupModalListeners };
