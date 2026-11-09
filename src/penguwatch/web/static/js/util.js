// Presentation helpers shared by several tabs.
import { state } from "./state.js";

// Shown in place of a poster that has no image or fails to load: an empty image, the CSS draws the background
// (--poster-bg), so the placeholder follows the theme and carries no colour of its own.
export const PLACEHOLDER_POSTER = "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='100' height='150'/>";

// --- Helpers ---

export function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// A line icon from the page's sprite (templates/icons.svg). `name` is a sprite id without the "i-" prefix.
export function icon(name, cls = "") {
  return `<svg class="i${cls ? ` ${cls}` : ""}" aria-hidden="true" focusable="false"><use href="#i-${name}"/></svg>`;
}

// Bars and meters carry their length as data-w="<percent>" in the markup (no style attribute: the CSP forbids
// inline styles); this sets it once the markup is in the page.
export function setWidths(root) {
  root.querySelectorAll("[data-w]").forEach((el) => { el.style.width = `${Number(el.dataset.w) || 0}%`; });
}

// English first: where an English title exists and differs from the stored one it is the main title, and the stored
// (romaji) title goes underneath in small text. `show` is anything with title and (optionally) en_title.
export function displayTitle(show) {
  return show.en_title && show.en_title !== show.title ? show.en_title : show.title;
}

// The small secondary line (stored title) under an English one, or "" when there is nothing to add.
export function subtitleHtml(show, tag = "div") {
  return show.en_title && show.en_title !== show.title ? `<${tag} class="title-alt">${esc(show.title)}</${tag}>` : "";
}

const MEDIA = { movie: ["film", "Movie"], anime: ["sparkle", "Anime"], show: ["tv", "TV show"] };

// The media type as a sprite icon (with its name for screen readers), for markup.
export function mediaIcon(mt) {
  const [name, label] = MEDIA[mt] || MEDIA.show;
  return `<span class="media-ic" title="${label}">${icon(name)}<span class="sr-only">${label}</span></span>`;
}

export const STATUS_LABELS = { watching: "Watching", plantowatch: "Plan to Watch", hold: "On Hold", completed: "Completed", dropped: "Dropped" };

export function statusOptionsFor(show) {
  const isMovie = show.media_type === "movie";
  return (isMovie ? ["plantowatch", "completed", "dropped"] : ["watching", "plantowatch", "hold", "completed", "dropped"])
    .map((st) => `<option value="${st}" ${show.status === st ? "selected" : ""}>${isMovie && st === "completed" ? "Watched" : STATUS_LABELS[st]}</option>`)
    .join("");
}

// Who watches: the household's people [{id, name}] in order, from the Household setting (household.members), which the
// server puts in a <meta> of the page. A title's `household` is the ids of the people who want it.
export const HOUSEHOLD = (() => {
  try {
    const members = JSON.parse(document.querySelector('meta[name="penguwatch-household"]')?.content || "[]");
    return Array.isArray(members) ? members.filter((m) => m && typeof m.id === "string") : [];
  } catch {
    return [];
  }
})();

// Two or more people: titles say who wants to watch them, and the "who" controls show. One person: they are hidden.
export const householdShared = HOUSEHOLD.length >= 2;
// Who a title is for when nobody says ("Add for me")
export const householdFirst = HOUSEHOLD.length ? HOUSEHOLD[0].id : "person1";

// The current members among `ids`, in the household's order.
export function householdIds(ids) {
  const set = new Set(ids || []);
  return HOUSEHOLD.filter((m) => set.has(m.id)).map((m) => m.id);
}

export function householdName(id) {
  return (HOUSEHOLD.find((m) => m.id === id) || {}).name || "";
}

// The word for titles two or more people want: "Both want" with two people, "Shared" with more.
export const sharedLabel = () => (HOUSEHOLD.length === 2 ? "Both want" : "Shared");

// Plain text for who wants a title (escape it before putting it into HTML): one person's name; "Both" (two people) or
// "Everyone" (three or more) when all of them do; otherwise the names, or "Ann +2" when they would not fit.
export function householdText(ids, fit = 18) {
  const who = householdIds(ids);
  if (!who.length) return HOUSEHOLD.length ? HOUSEHOLD[0].name : "";
  if (who.length === 1) return householdName(who[0]);
  if (who.length === HOUSEHOLD.length) return HOUSEHOLD.length === 2 ? "Both" : "Everyone";
  const names = who.map(householdName);
  const all = names.join(", ");
  return all.length <= fit ? all : `${names[0]} +${names.length - 1}`;
}

// The icon for who wants a title: one person or several.
export const householdIcon = (ids) => (householdIds(ids).length > 1 ? "users" : "user");

// Toggle chips, one per person, for choosing who wants a title (a checkbox each; at least one stays ticked: see
// keepOneTicked). `name` groups the checkboxes; `label` is the group's accessible name.
export function whoToggles(ids, { name = "who", label = "Who wants to watch", attrs = "" } = {}) {
  const on = new Set(householdIds(ids).length ? householdIds(ids) : [householdFirst]);
  const toggle = (m) => {
    const tick = on.has(m.id) ? " checked" : "";
    return `<label class="who-toggle"><input type="checkbox" name="${esc(name)}" value="${esc(m.id)}"${tick}><span>${esc(m.name)}</span></label>`;
  };
  return `<span class="who-toggles" role="group" aria-label="${esc(label)}"${attrs}>${HOUSEHOLD.map(toggle).join("")}</span>`;
}

// The ids ticked in a group of whoToggles, in the household's order.
export function tickedWho(group) {
  return householdIds([...group.querySelectorAll('input[type="checkbox"]:checked')].map((i) => i.value));
}

// A change of one toggle that would leave nobody ticked is undone: a title is always wanted by someone. True when the
// change stands.
export function keepOneTicked(input) {
  const group = input.closest(".who-toggles");
  if (!input.checked && group && !group.querySelector('input[type="checkbox"]:checked')) {
    input.checked = true;
    showToast("At least one person wants a title", true);
    return false;
  }
  return true;
}

// The built-in rating sources a title has a value for (state.ratingSources lists them; a title keeps each in its
// `<source id>_rating` column). IMDb first, as before.
export function ratingBadges(show) {
  const builtin = (state.ratingSources || []).filter((s) => s.builtin);
  builtin.sort((a, b) => (a.id === "imdb" ? -1 : 0) - (b.id === "imdb" ? -1 : 0));
  return builtin
    .filter((s) => show[s.id + "_rating"])
    .map((s) => `<span class="rating" title="${esc(s.label)} rating"><b>${esc(s.label)}</b> ${Number(show[s.id + "_rating"]).toFixed(1)}</span>`)
    .join("");
}

// Badges for the custom rating sources a title has a value for: icon, the source's short label, the value. `sources` is
// state.ratingSources; `onlyCards` keeps those marked "show on cards". Names and labels are user input: escaped.
export function customRatingBadges(show, sources, onlyCards = false) {
  return (sources || [])
    .filter((s) => !s.builtin && (!onlyCards || s.show_on_cards) && show.ratings && show.ratings[s.id])
    .map((s) => {
      const r = show.ratings[s.id];
      return `<span class="rating" title="${esc(s.name)}">${icon("star", "sm")}<b>${esc(s.label)}</b> ${esc(Number(r.value).toFixed(s.decimals))}</span>`;
    })
    .join("");
}

// The values of the ticked checkboxes with this class (review and tidy lists).
export function checkedValues(cls) {
  return [...document.querySelectorAll(`.${cls}:checked`)].map((c) => c.value);
}

// A "select all" checkbox (id masterId) for the row checkboxes of class `cls`: ticking it ticks every row, clearing it
// clears them, and it shows ticked, clear or mixed as rows change. Call syncSelectAll after re-rendering the rows.
export function wireSelectAll(masterId, cls) {
  const master = document.getElementById(masterId);
  if (!master) return;
  master.addEventListener("change", () => {
    document.querySelectorAll(`.${cls}`).forEach((c) => { c.checked = master.checked; });
    master.indeterminate = false;
  });
  document.addEventListener("change", (e) => {
    if (e.target instanceof HTMLInputElement && e.target.classList.contains(cls)) syncSelectAll(masterId, cls);
  });
}

export function syncSelectAll(masterId, cls) {
  const master = document.getElementById(masterId);
  if (!master) return;
  const boxes = [...document.querySelectorAll(`.${cls}`)];
  const ticked = boxes.filter((c) => c.checked).length;
  master.checked = boxes.length > 0 && ticked === boxes.length;
  master.indeterminate = ticked > 0 && ticked < boxes.length;
  master.disabled = boxes.length === 0;
}

// --- Toast ---

export function showToast(message, isError = false) {
  const toast = document.getElementById("toast");
  if (!toast) return;
  toast.innerHTML = `${icon(isError ? "warn" : "ok")}<span>${esc(message)}</span>`;
  toast.classList.toggle("bad", isError);
  toast.classList.add("on");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => toast.classList.remove("on"), 3500);
}
