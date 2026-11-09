// Library card markup. The card shows the current status / stream / rating / audience / priority as small
// buttons; the real selector is built only when one is activated (data-action="edit-field") and removed again
// when it is used, left, or dismissed with Escape. This keeps a card at ~40 DOM nodes instead of ~80. "Who wants to
// watch" is a set of people: its editor is a row of toggles, applied when it closes (Done, or focus leaving it).
import { registerAction } from "../../js/dispatch.js";
import {
  changeShowHousehold,
  changeShowPriority,
  changeShowRating,
  changeShowService,
  changeShowStatus,
} from "../../js/showchanges.js";
import { state } from "../../js/state.js";
import { syncsTo, syncsToLibrary } from "../../js/drivers.js";
import {
  STATUS_LABELS, customRatingBadges, displayTitle, esc, householdIcon, householdIds, householdShared, householdText, icon, keepOneTicked,
  mediaIcon, ratingBadges, subtitleHtml, tickedWho, whoToggles,
} from "../../js/util.js";

const RATINGS = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1];
const PRIORITY = [["high", "High"], ["medium", "Normal"], ["low", "Low"]];
const MEDIA_ICONS = { movie: "film", anime: "sparkle", show: "tv" };
const ratingValue = (s) => (s.user_rating ? String(Math.round(s.user_rating)) : "");

function statusOptions(show) {
  const movie = show.media_type === "movie";
  return (movie ? ["plantowatch", "completed", "dropped"] : ["watching", "plantowatch", "hold", "completed", "dropped"])
    .map((st) => [st, movie && st === "completed" ? "Watched" : STATUS_LABELS[st]]);
}

const labelOf = (options, value) => (options.find(([v]) => v === value) || [0, value])[1];

// What each editable field reads, offers and calls. `chip` is the button text; `name` labels the editor; `icon` is
// the sprite drawn before the text.
const FIELDS = {
  status: {
    get name() { return `Status${syncsTo()}`; }, value: (s) => s.status, options: statusOptions, apply: changeShowStatus,
    chip: (s) => labelOf(statusOptions(s), s.status), icon: () => "",
  },
  household: {
    name: "Who wants to watch", value: (s) => householdIds(s.household), multi: true, apply: changeShowHousehold,
    chip: (s) => householdText(s.household), icon: (s) => householdIcon(s.household),
  },
  priority: {
    name: "Priority", value: (s) => s.priority, options: () => PRIORITY, apply: changeShowPriority,
    chip: (s) => labelOf(PRIORITY, s.priority), icon: (s) => (s.priority === "high" ? "fire" : ""),
  },
  rating: {
    get name() { return `Your rating${syncsTo()}`; }, value: ratingValue, apply: changeShowRating,
    options: () => [["", "Your rating"], ...RATINGS.map((r) => [String(r), `${r}/10`])],
    chip: (s) => (s.user_rating ? `${ratingValue(s)}/10` : "Your rating"), icon: () => "star",
  },
  service: {
    name: "Streaming service", value: (s) => s.service_id || "", apply: changeShowService,
    options: () => [["", "(Unassigned / Other)"], ...state.services.map((sv) => [sv.id, sv.name])],
    chip: (s) => (s.service_id ? (state.services.find((sv) => sv.id === s.service_id) || { name: s.service_id }).name : "(Unassigned / Other)"),
    icon: () => "tv",
  },
};

function chip(show, field, extraClass = "") {
  const def = FIELDS[field];
  const text = def.chip(show);
  const ic = def.icon(show);
  const tip = field === "service" ? (show.service_manual ? "Chosen by you" : "Matched automatically") : def.name;
  return `<button type="button" class="chip-btn ${extraClass}" data-action="edit-field" data-field="${field}" data-id="${show.id}" title="${esc(tip)}" aria-label="${esc(`${def.name}: ${text}. Change for ${displayTitle(show)}`)}">${ic ? icon(ic, "sm") : ""}<span class="chip-text">${esc(text)}</span>${icon("chevron", "sm chip-caret")}</button>`;
}

function interestBadge(show) {
  if (typeof show.interest_score !== "number") return "";
  const reasons = (show.interest_reasons || []).join(" · ");
  return `<span class="chip interest-badge" title="${esc(reasons || "Likely interest")}" aria-label="Likely interest ${show.interest_score} out of 100">${icon("sparkle")}${show.interest_score}</span>`;
}

function progressHtml(show, isMovie, isCompleted, totalAired, watched) {
  const pct = totalAired > 0 ? Math.min(100, Math.round((watched / totalAired) * 100)) : 0;
  if (isMovie) {
    return `<div class="progress-stepper-row">
           <button class="btn sm ${isCompleted ? "" : "outline"} grow" data-call="toggleMovieWatched" data-args='[${show.id}, ${!isCompleted}]'>
             ${isCompleted ? `${icon("check")}Watched (undo)` : `${icon("film")}Mark watched`}
           </button>
         </div>`;
  }
  return `<div class="progress-container">
          <div class="progress-labels num">
            <span>${isCompleted ? `${icon("check", "sm ink-good")}${show.status === "completed" ? "Completed" : "Caught up"}` : `Ep ${watched} of ${totalAired > 0 ? totalAired : "?"}`}</span>
            <span>${isCompleted ? `${watched} eps` : totalAired > 0 ? `${pct}% (${Math.max(0, totalAired - watched)} left)` : `${watched} eps`}</span>
          </div>
          <div class="progress ${isCompleted ? "done" : ""}"><i data-w="${isCompleted ? 100 : pct}"></i></div>
        </div>
        <div class="progress-stepper-row">
          <div class="stepper-cluster" title="Episodes">
            <span class="stepper-cluster-lbl">Ep</span>
            <button class="btn-step" data-call="updateShowProgress" data-args='[${show.id}, {"episode_delta": -1}]' title="Unmark the last watched episode" aria-label="One episode back">${icon("minus", "sm")}</button>
            <button class="btn-step btn-step-inc" data-call="updateShowProgress" data-args='[${show.id}, {"episode_delta": 1}]' title="Mark the next episode watched" aria-label="One episode on">${icon("plus", "sm")}</button>
          </div>
          <div class="stepper-cluster" title="Whole seasons">
            <span class="stepper-cluster-lbl">Season</span>
            <button class="btn-step" data-call="updateShowProgress" data-args='[${show.id}, {"season_delta": -1}]' aria-label="One season back">${icon("minus", "sm")}</button>
            <button class="btn-step btn-step-inc" data-call="updateShowProgress" data-args='[${show.id}, {"season_delta": 1}]' aria-label="One season on">${icon("plus", "sm")}</button>
          </div>
          <button class="btn-step btn-step-edit" data-call="openEpisodes" data-args='[${show.id}]' title="Episode checklist">${icon("checklist", "sm")}Episodes</button>
        </div>`;
}

function cardHtml(show) {
  const isMovie = show.media_type === "movie";
  const totalAired = show.aired_episodes || show.total_episodes || 0;
  const watched = show.watched_episodes || 0;
  const isCompleted = show.status === "completed" || (totalAired > 0 && watched >= totalAired);
  const fallback = `<div class="poster-fallback show-poster-fallback"${show.poster ? " hidden" : ""}>${icon(MEDIA_ICONS[show.media_type] || "tv")}</div>`;
  return `
      <div class="show-poster-wrap clickable" data-call="openShow" data-args='[${show.id}]' title="Details">
        ${show.poster ? `<img class="poster show-poster" src="${esc(show.poster)}" alt="" loading="lazy" data-fallback="swap">` : ""}
        ${fallback}
        <div class="poster-top-badges">
          <span class="service-tag on-image">${esc(show.network || (isMovie ? "Movie" : "Other"))}</span>
          <span class="ratings on-image-ratings">${ratingBadges(show)}${customRatingBadges(show, state.ratingSources, true)}</span>
        </div>
        ${show.is_ready_to_binge && !isMovie ? `<span class="binge-floating-tag badge on-image">${icon("zap")}Ready to binge</span>` : ""}
      </div>
      <div class="show-info">
        <div class="show-title-row">
          <h3 class="show-title clickable" data-call="openShow" data-args='[${show.id}]'>${mediaIcon(show.media_type)} ${esc(displayTitle(show))} <span class="show-year">${show.year ? `(${show.year})` : ""}</span></h3>
          ${subtitleHtml(show)}
        </div>
        <div class="show-tags-row">
          ${householdShared ? `<span class="chip who-chip" title="${esc(householdText(show.household, 200))}">${icon(householdIcon(show.household))}<span class="who-chip-t">${esc(householdText(show.household))}</span></span>` : ""}
          <span class="chip prio-${esc(show.priority)}">${show.priority === "high" ? icon("fire") : ""}${esc(labelOf(PRIORITY, show.priority))}</span>
          <span class="chip">${esc(STATUS_LABELS[show.status] || show.status)}</span>
          ${interestBadge(show)}
        </div>
        ${
          !isMovie && (show.next_to_watch || show.last_watched)
            ? `<div class="next-watch-row">
                 ${show.next_to_watch ? `<span class="next-pill">${icon("play", "sm")}Up next: <strong>${esc(show.next_to_watch)}</strong></span>` : ""}
                 ${show.last_watched ? `<span class="last-pill">${icon("check", "sm")}Last: ${esc(show.last_watched)}</span>` : ""}
               </div>`
            : ""
        }
        <div class="card-service-row">
          <span class="service-select-label">Stream</span>
          ${chip(show, "service", "chip-grow")}
        </div>
        ${progressHtml(show, isMovie, isCompleted, totalAired, watched)}
        <div class="show-card-actions">
          ${chip(show, "status")}${householdShared ? chip(show, "household") : ""}${chip(show, "priority")}
        </div>
        <div class="show-card-actions">
          ${chip(show, "rating", "chip-grow")}
          <button class="btn icon sm card-remove" data-action="delete-show" data-id="${show.id}" data-title="${esc(displayTitle(show))}" data-linked="${syncsToLibrary(show) ? "true" : "false"}" title="Remove" aria-label="Remove ${esc(displayTitle(show))}">${icon("trash")}</button>
        </div>
      </div>`;
}

function setBars(card) {
  card.querySelectorAll("[data-w]").forEach((el) => { el.style.width = `${el.dataset.w}%`; });
}

export function createCard(show) {
  const card = document.createElement("div");
  card.className = "show-card";
  card.dataset.showId = String(show.id);
  card.innerHTML = cardHtml(show);
  setBars(card);
  return card;
}

// Re-render one card in place (same element, so the scroll position holds); keyboard focus stays on its chip.
export function fillCard(card, show) {
  const field = card.contains(document.activeElement) ? document.activeElement.dataset.field : null;
  card.innerHTML = cardHtml(show);
  setBars(card);
  if (field) card.querySelector(`[data-field="${field}"]`)?.focus();
}

// --- On-demand editor ---

// Who wants to watch: a toggle per person in place of the chip; the set is saved when the editor closes (Done, Enter, or
// focus leaving it), not per toggle, so choosing three people is one change. Escape closes without saving.
function openWhoEditor(btn, def, show) {
  const box = document.createElement("span");
  box.className = "who-editor";
  box.innerHTML = `${whoToggles(def.value(show), { name: `who-${show.id}`, label: `${def.name} for ${displayTitle(show)}` })}<button type="button" class="btn icon sm who-done" aria-label="Done">${icon("check", "sm")}</button>`;
  const group = box.querySelector(".who-toggles");
  let closed = false;
  const close = (save, refocus) => {
    if (closed) return;
    closed = true;
    const ids = tickedWho(group);
    box.replaceWith(btn);
    if (refocus) btn.focus();
    if (save && ids.length && ids.join() !== def.value(show).join()) def.apply(show.id, ids);
  };
  group.addEventListener("change", (ev) => keepOneTicked(ev.target));
  box.querySelector(".who-done").addEventListener("click", () => close(true, true));
  box.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") { ev.preventDefault(); close(false, true); }
    if (ev.key === "Enter") { ev.preventDefault(); close(true, true); }
  });
  // Deferred: focus moves between the toggles through the body on some browsers
  box.addEventListener("focusout", () => setTimeout(() => { if (!box.contains(document.activeElement)) close(true, false); }, 200));
  btn.replaceWith(box);
  group.querySelector("input")?.focus();
}

function openEditor(btn) {
  const def = FIELDS[btn.dataset.field];
  const show = state.shows.find((s) => s.id === Number(btn.dataset.id));
  if (!def || !show) return;
  if (def.multi) { openWhoEditor(btn, def, show); return; }
  const select = document.createElement("select");
  select.className = "input sm chip-editor";
  select.setAttribute("aria-label", `${def.name} for ${displayTitle(show)}`);
  for (const [value, text] of def.options(show)) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = text;
    select.appendChild(opt);
  }
  select.value = def.value(show);
  let closed = false;
  const close = (refocus) => {
    if (closed) return;
    closed = true;
    select.replaceWith(btn);
    if (refocus) btn.focus();
  };
  select.addEventListener("change", () => {
    const value = select.value;
    close(true);
    if (value !== def.value(show)) def.apply(show.id, value);
  });
  // Deferred so a picker that blurs the select just before it reports "change" (some mobile browsers) still lands
  select.addEventListener("blur", () => setTimeout(() => close(false), 200));
  select.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    ev.preventDefault();
    close(true);
  });
  btn.replaceWith(select);
  select.focus();
  try { select.showPicker?.(); } catch { /* needs a user gesture on some browsers; the select is still focused */ }
}

registerAction("edit-field", (btn) => openEditor(btn));
