// Show detail / episodes checklist dialog, and the "set the watched count" dialog it opens. Every tab opens it with
// data-call="openShow" (or "openEpisodes"); what changes in here is told to the page with "show-updated".
import { api } from "../../js/api.js";
import { emit, on } from "../../js/bus.js";
import { afterShowChange, loadRatingSources, scheduleSideRefresh } from "../../js/data.js";
import { register, registerAction } from "../../js/dispatch.js";
import { closeModal, isOpen, onModalClose, openModal } from "../../js/modal.js";
import { attributionsFor, creditLink, syncingTo, syncsToLibrary, titleLinks } from "../../js/drivers.js";
import { updateShowProgress } from "../../js/showchanges.js";
import { state } from "../../js/state.js";
import {
  PLACEHOLDER_POSTER,
  STATUS_LABELS,
  displayTitle,
  esc,
  householdOptions,
  householdShared,
  icon,
  mediaIcon,
  ratingBadges,
  showToast,
  statusOptionsFor,
  subtitleHtml,
} from "../../js/util.js";

const MEDIA = { movie: ["film", "Movie"], anime: ["sparkle", "Anime"], show: ["tv", "TV Show"] };
let episodesShowId = null;   // the title the dialog shows
let progressShow = { showId: null, watched: 0, totalAired: 0 };

// --- Episodes checklist ---

function closeEpisodes() {
  closeModal("episodes-modal");
}

function openEpisodes(showId) {
  return openShow(showId);
}

async function openShow(showId) {
  episodesShowId = showId;
  closeModal("progress-modal");
  openModal("episodes-modal");
  document.getElementById("show-detail").innerHTML = "";
  document.getElementById("episodes-summary").innerHTML = "";
  document.getElementById("episodes-body").innerHTML = '<div class="empty loading-state"><span class="spinner" aria-hidden="true"></span><span>Loading…</span></div>';
  await loadEpisodes(showId);
}

async function loadEpisodes(showId, refresh = false) {
  try {
    const data = await api(`/api/shows/${showId}/episodes${refresh ? "?refresh=true" : ""}`);
    renderEpisodes(data);
  } catch (err) {
    document.getElementById("episodes-body").innerHTML = `<div class="empty st st-crit">${icon("warn")}<span>${esc(err.message)}</span></div>`;
  }
}

function renderShowDetail(show) {
  const isMovie = show.media_type === "movie";
  const genres = (show.genres || []).filter((g) => g !== "Anime").slice(0, 6).map(esc).join(" · ");
  const serviceOptions = state.services
    .map((s) => `<option value="${esc(s.id)}" ${s.id === show.service_id ? "selected" : ""}>${esc(s.name)}${s.is_active ? " (active)" : ""}</option>`)
    .join("");
  const ratingOptions = ['<option value="">—</option>']
    .concat([10, 9, 8, 7, 6, 5, 4, 3, 2, 1].map((r) => `<option value="${r}" ${Math.round(show.user_rating || 0) === r ? "selected" : ""}>${r}/10</option>`))
    .join("");
  const links = [
    ...titleLinks(show).map((l) => `<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.label)}${icon("external", "sm")}</a>`),
    (show.ids || {}).imdb ? `<a href="https://www.imdb.com/title/${esc(show.ids.imdb)}/" target="_blank" rel="noopener">IMDb${icon("external", "sm")}</a>` : "",
  ].filter(Boolean).join(" ");
  const facts = [
    isMovie ? `${show.runtime_minutes} min` : `${show.aired_episodes || "?"} episodes aired`,
    !isMovie && show.is_ended ? "Ended" : !isMovie ? "Continuing" : "",
    show.network ? esc(show.network) : "",
  ].filter(Boolean).join(" · ");

  const box = document.getElementById("show-detail");
  box.innerHTML = `
    <div class="detail-hero${show.banner ? " has-banner" : ""}">
      <img class="poster detail-poster" src="${esc(show.poster || PLACEHOLDER_POSTER)}" alt="" data-fallback="poster">
      <div class="detail-main">
        <div class="detail-title">${mediaIcon(show.media_type)} ${esc(displayTitle(show))} <span class="show-year">${show.year ? `(${show.year})` : ""}</span></div>
        ${subtitleHtml(show)}
        <div class="detail-facts">${facts}</div>
        ${genres ? `<div class="detail-facts">${genres}</div>` : ""}
        <div class="detail-badges">${ratingBadges(show)} ${links ? `<span class="detail-links">${links}</span>` : ""}</div>
        <p class="detail-overview">${esc(show.overview || "No overview available.")}</p>
      </div>
    </div>
    <p class="fine detail-credits" id="detail-credits" hidden></p>
    <div class="detail-controls">
      <label>Status<select class="input" data-call="detailPatchField" data-on="change" data-args='[${show.id}, "status", "$value"]'>${statusOptionsFor(show)}</select></label>
      <label>Your rating<select class="input" data-call="detailRate" data-on="change" data-args='[${show.id}, "$value"]'>${ratingOptions}</select></label>
      ${householdShared ? `<label>Who's watching<select class="input" data-call="detailPatchField" data-on="change" data-args='[${show.id}, "household_tag", "$value"]'>
        ${householdOptions().map(([tag, label]) => `<option value="${tag}" ${show.household_tag === tag ? "selected" : ""}>${esc(label)}</option>`).join("")}</select></label>` : ""}
      <label>Priority<select class="input" data-call="detailPatchField" data-on="change" data-args='[${show.id}, "priority", "$value"]'>
        <option value="high" ${show.priority === "high" ? "selected" : ""}>High</option>
        <option value="medium" ${show.priority === "medium" ? "selected" : ""}>Normal</option>
        <option value="low" ${show.priority === "low" ? "selected" : ""}>Low</option></select></label>
      <label>Streams on<select class="input" data-call="detailPatchField" data-on="change" data-args='[${show.id}, "service_id", "$value"]'>
        <option value="">(Unassigned)</option>${serviceOptions}</select></label>
      <label class="detail-notes">Notes<textarea class="input" rows="2" placeholder="e.g. waiting for season 3, watch with the kids…"
        data-call="detailPatchField" data-on="change" data-args='[${show.id}, "notes", "$value"]'>${esc(show.notes || "")}</textarea></label>
    </div>
    <section class="detail-ratings" id="show-ratings" aria-label="Ratings"></section>
    <div class="detail-actions">
      ${isMovie ? `<button class="btn sm outline" data-call="detailPatchField" data-args='[${show.id}, "status", "${show.status === "completed" ? "plantowatch" : "completed"}"]'>${show.status === "completed" ? `${icon("check")}Watched (undo)` : `${icon("film")}Mark watched`}</button>` : ""}
      <button class="btn sm danger" data-action="delete-show" data-id="${show.id}" data-title="${esc(displayTitle(show))}" data-linked="${syncsToLibrary(show) ? "true" : "false"}">${icon("trash")}Remove</button>
    </div>`;
  const credits = attributionsFor(show);
  const creditBox = document.getElementById("detail-credits");
  credits.forEach((a, i) => creditBox.append(i ? " · " : "", creditLink(a)));
  creditBox.hidden = !credits.length;
  loadShowRatings(show.id);
  // The banner behind the hero, as a style property (the CSP forbids style attributes in markup)
  const hero = box.querySelector(".detail-hero");
  if (show.banner) hero.style.backgroundImage = `url("${String(show.banner).replace(/["\\\n\r]/g, encodeURIComponent)}")`;
}

// --- Ratings: every source on this title; the custom ones can be set and cleared here ---

let shownRatings = {};   // source id -> the row the section shows (a save keeps its votes and link)

const safeLink = (url) => (/^https?:\/\//i.test(url || "") ? url : null);

async function loadShowRatings(showId) {
  const box = document.getElementById("show-ratings");
  if (!box) return;
  try {
    renderShowRatings(showId, await api(`/api/shows/${showId}/ratings`));
  } catch (err) {
    box.innerHTML = `<div class="empty st st-crit">${icon("warn")}<span>${esc(err.message)}</span></div>`;
  }
}

function renderShowRatings(showId, rows) {
  shownRatings = Object.fromEntries(rows.map((r) => [r.source, r]));
  const fmt = (r) => (r.value == null ? "—" : Number(r.value).toFixed(r.decimals));
  const body = rows.map((r) => {
    const link = safeLink(r.url);
    const votes = r.votes ? ` <span class="muted">${esc(Number(r.votes).toLocaleString())} votes</span>` : "";
    const edit = r.builtin ? `<span class="muted">built in</span>` : `
      <input type="number" class="input sm num rr-input" id="rr-in-${esc(r.source)}" min="${esc(r.scale_min)}" max="${esc(r.scale_max)}" step="any"
        value="${r.value == null ? "" : esc(r.value)}" aria-label="${esc(r.name)} value, ${esc(r.scale_min)} to ${esc(r.scale_max)}">
      <button type="button" class="btn sm" data-action="rating-save" data-show="${showId}" data-source="${esc(r.source)}">${icon("check")}Save</button>
      <button type="button" class="btn sm outline" data-action="rating-clear" data-show="${showId}" data-source="${esc(r.source)}" ${r.value == null ? "disabled" : ""}>Clear</button>`;
    return `<div class="rating-row">
      <span class="rr-name"><span class="rating"><b>${esc(r.label)}</b></span> ${esc(r.name)}</span>
      <span class="rr-value num">${esc(fmt(r))}${r.value == null ? "" : `<span class="muted"> / ${esc(r.scale_max)}</span>`}${votes}${link ? ` <a href="${esc(link)}" target="_blank" rel="noopener noreferrer">${icon("external", "sm")}<span class="sr-only">Open ${esc(r.name)} page</span></a>` : ""}</span>
      <span class="rr-edit">${edit}</span>
    </div>`;
  }).join("");
  document.getElementById("show-ratings").innerHTML = `<h3 class="detail-sub">${icon("star", "sm")}Ratings</h3><div class="rating-rows">${body}</div>`;
}

async function afterRatingChange(showId) {
  await loadShowRatings(showId);
  try {
    afterShowChange(await api(`/api/shows/${showId}`));   // the library card shows the new badge
  } catch (err) {
    console.error("refresh after a rating change:", err);
  }
  loadRatingSources();                                      // the Ratings tab's counts
}

async function saveRating(el) {
  const { show, source } = el.dataset;
  const input = document.getElementById(`rr-in-${source}`);
  const raw = input.value.trim();
  if (raw === "" || !Number.isFinite(Number(raw))) return showToast("Enter a number first", true);
  const cur = shownRatings[source] || {};
  try {
    await api(`/api/shows/${show}/ratings/${encodeURIComponent(source)}`, {
      method: "PUT", body: { value: Number(raw), votes: cur.votes ?? null, url: cur.url ?? null },
    });
    showToast("Rating saved");
    await afterRatingChange(show);
  } catch (err) {
    showToast(err.message, true);
  }
}

async function clearRating(el) {
  const { show, source } = el.dataset;
  try {
    await api(`/api/ratings/${encodeURIComponent(source)}/${show}`, { method: "DELETE" });
    showToast("Rating removed");
    await afterRatingChange(show);
  } catch (err) {
    showToast(err.message, true);
  }
}

function detailPatchField(showId, field, value) {
  return detailPatch(showId, { [field]: value });
}

function detailRate(showId, value) {
  return detailPatch(showId, value ? { user_rating: parseInt(value, 10) } : { clear_user_rating: true });
}

async function detailPatch(showId, body) {
  try {
    await api(`/api/shows/${showId}`, { method: "PATCH", body });
    const label = body.status ? `Status: ${STATUS_LABELS[body.status]}` : "Saved";
    showToast(`${label}${body.status || body.user_rating || body.clear_user_rating ? syncingTo() : ""}`);
    await loadEpisodes(showId);
    scheduleSideRefresh();
  } catch (err) {
    showToast(err.message, true);
  }
}

function renderEpisodes(data) {
  const show = data.show;
  emit("show-updated", show);   // keeps the library card in step with whatever changed in here
  renderShowDetail(show);
  const [ic, kind] = MEDIA[show.media_type] || MEDIA.show;
  document.getElementById("episodes-modal-title").textContent = kind;
  document.querySelector("#episodes-modal-icon use")?.setAttribute("href", `#i-${ic}`);
  if (show.media_type === "movie") {
    document.getElementById("episodes-summary").innerHTML = "";
    document.getElementById("episodes-body").innerHTML = "";
    return;
  }
  document.getElementById("episodes-summary").innerHTML = `
    <span class="num">${show.watched_episodes} of ${show.aired_episodes || "?"} aired episodes watched</span>
    ${show.next_to_watch ? `<span class="next-pill">${icon("play", "sm")}Up next: <strong>${esc(show.next_to_watch)}</strong></span>` : ""}
    ${show.next_episode_air_date ? `<span class="muted">Next new episode: ${esc(show.next_episode_air_date)}</span>` : ""}
    <span class="grow"></span>
    <button class="btn sm outline" data-action="progress-prompt" data-id="${show.id}" data-title="${esc(displayTitle(show))}" data-watched="${show.watched_episodes}" data-aired="${show.aired_episodes || show.total_episodes || 0}">${icon("edit")}Set count</button>
    <button class="btn sm" data-call="loadEpisodes" data-args='[${show.id}, true]'>${icon("refresh")}Refresh list</button>`;
  const body = document.getElementById("episodes-body");
  if (!data.seasons.length) {
    body.innerHTML = `<div class="empty">${icon("info")}<span>${
      Object.keys(show.ids || {}).length ? "No episode list is available for this title yet." : "This title isn't linked to a catalogue, so only the episode count is tracked."
    }</span></div>`;
    return;
  }
  const currentSeason = (() => {
    const m = /^S(\d+)/i.exec(show.next_to_watch || "");
    return m ? parseInt(m[1], 10) : data.seasons[data.seasons.length - 1].number;
  })();
  body.innerHTML = data.seasons
    .map((season) => {
      const done = season.aired > 0 && season.watched >= season.aired;
      const eps = season.episodes
        .map(
          (e) => `
          <label class="ep-row ${e.is_watched ? "ep-watched" : ""} ${e.aired ? "" : "ep-future"}">
            <input type="checkbox" ${e.is_watched ? "checked" : ""} ${e.aired ? "" : "disabled"}
              data-call="toggleEpisode" data-on="change" data-args='[${show.id}, ${e.season}, ${e.number}, "$checked"]'>
            <span class="ep-num">E${String(e.number).padStart(2, "0")}</span>
            <span class="ep-title">${esc(e.title || "Episode " + e.number)}</span>
            <span class="ep-date">${e.air_date ? esc(e.air_date.slice(0, 10)) : ""}</span>
          </label>`
        )
        .join("");
      return `
        <details class="season-block" ${season.number === currentSeason ? "open" : ""}>
          <summary>
            <span class="season-name">Season ${season.number}</span>
            <span class="season-progress ${done ? "done" : ""}">${done ? icon("check", "sm") : ""}${season.watched}/${season.aired}${season.episodes.length > season.aired ? ` (+${season.episodes.length - season.aired} upcoming)` : ""}</span>
            <button class="btn sm ${done ? "" : "outline"}" data-call="toggleSeason" data-prevent="1" data-args='[${show.id}, ${season.number}, ${!done}]'>
              ${done ? "Unmark season" : "Mark season watched"}
            </button>
          </summary>
          <div class="ep-list">${eps}</div>
        </details>`;
    })
    .join("");
}

async function toggleEpisode(showId, season, number, watched) {
  try {
    await api(`/api/shows/${showId}/episodes/toggle`, { method: "POST", body: { season, number, watched } });
    await loadEpisodes(showId);
    scheduleSideRefresh();
  } catch (err) {
    showToast(err.message, true);
  }
}

async function toggleSeason(showId, season, watched) {
  try {
    await api(`/api/shows/${showId}/seasons/toggle`, { method: "POST", body: { season, watched } });
    showToast(`Season ${season} ${watched ? "marked watched" : "unmarked"}`);
    await loadEpisodes(showId);
    scheduleSideRefresh();
  } catch (err) {
    showToast(err.message, true);
  }
}

// --- Progress dialog ---

function openProgressPrompt(showId, title, watched, totalAired) {
  progressShow = { showId, watched, totalAired };
  document.getElementById("pm-show-id").value = showId;
  document.getElementById("progress-modal-title").textContent = `Update progress: ${title}`;
  document.getElementById("pm-show-info").textContent = `Currently watched ${watched} of ${totalAired > 0 ? totalAired : "?"} aired episodes.`;
  const input = document.getElementById("pm-watched-input");
  input.value = watched;
  if (totalAired > 0) input.max = totalAired;
  else input.removeAttribute("max");
  openModal("progress-modal");
  input.focus();
  input.select();
}

function setupProgress() {
  const pmInput = document.getElementById("pm-watched-input");
  document.getElementById("pm-btn-prev-ep")?.addEventListener("click", () => (pmInput.value = Math.max(0, (parseInt(pmInput.value, 10) || 0) - 1)));
  document.getElementById("pm-btn-next-ep")?.addEventListener("click", () => (pmInput.value = (parseInt(pmInput.value, 10) || 0) + 1));
  document.getElementById("pm-btn-reset-zero")?.addEventListener("click", () => (pmInput.value = 0));
  document.getElementById("pm-btn-episodes")?.addEventListener("click", () => openEpisodes(progressShow.showId));
  const save = async () => {
    const showId = parseInt(document.getElementById("pm-show-id").value, 10);
    const count = parseInt(pmInput.value, 10);
    if (!isNaN(showId) && !isNaN(count)) {
      await updateShowProgress(showId, { watched_episodes: Math.max(0, count) });
      closeModal("progress-modal");
      if (episodesShowId === showId && isOpen("episodes-modal")) loadEpisodes(showId);
    }
  };
  document.getElementById("pm-btn-save")?.addEventListener("click", save);
  pmInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      save();
    }
  });
}

register({
  openShow, openEpisodes, toggleSeason, toggleEpisode, loadEpisodes, detailPatchField, detailRate,
});
registerAction("progress-prompt", (el) => {
  const d = el.dataset;
  openProgressPrompt(Number(d.id), d.title, Number(d.watched), Number(d.aired));
});
registerAction("rating-save", saveRating);
registerAction("rating-clear", clearRating);
onModalClose("episodes-modal", () => { episodesShowId = null; });
// The title shown here was removed (its Remove button): the dialog closes
on("show-removed", (id) => { if (id === episodesShowId) closeEpisodes(); });

export default { id: "details", mount: setupProgress };
