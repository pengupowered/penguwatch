// Up Next tab: what to watch next for every show in progress.
import { api, showLoading } from "../../js/api.js";
import { on } from "../../js/bus.js";
import { afterShowChange, loadStats, registerLoader } from "../../js/data.js";
import { register, registerAction } from "../../js/dispatch.js";
import { syncsTo } from "../../js/drivers.js";
import { state } from "../../js/state.js";
import { onTabActivated } from "../../js/tabs.js";
import {
  HOUSEHOLD, PLACEHOLDER_POSTER, displayTitle, esc, householdShared, householdText, icon, setWidths, sharedLabel, showToast, statusOptionsFor,
  subtitleHtml,
} from "../../js/util.js";

const TAB = "upnext-tab";

// Filter chips: "active" asks the server for titles on the services you have now, the others filter the returned list
// in the browser (`extra`): aired today, one chip per person ("m:<id>": the titles that person wants alone) and the shared
// titles (two or more people; "Both want" with two; its key is still "both", as before 0.17).
const CHIPS = [["all", "All"], ["active", "On your services"], ["today", "Aired today"],
  ...(householdShared ? [...HOUSEHOLD.map((m) => [`m:${m.id}`, m.name]), ["both", sharedLabel()]] : [])];
let extra = "";
let onlyActive = false;
let counts = {};
let statsStale = false;

const localToday = () => new Date().toLocaleDateString("en-CA");

function setChip(name) {
  extra = name === "today" || name === "both" || name.startsWith("m:") ? name : "";
  onlyActive = name === "active";
  fetchUpNext();
}

function airsToday(s) {
  const nxt = s.up_next;
  return Boolean(nxt && s.episodes_left > 0 && nxt.air_date && String(nxt.air_date).slice(0, 10) === localToday());
}

const wantedBy = (s) => s.household || [];
// The titles a browser-side chip keeps
function chipFilter(name) {
  if (name === "today") return airsToday;
  if (name === "both") return (s) => wantedBy(s).length >= 2;
  if (name.startsWith("m:")) return (s) => wantedBy(s).length === 1 && wantedBy(s)[0] === name.slice(2);
  return () => true;
}

function renderChips(items) {
  const box = document.getElementById("upnext-chips");
  if (!box) return;
  if (onlyActive) counts.active = items.length;
  else {
    counts = { all: items.length, active: items.filter((s) => s.service_active).length };
    for (const [key] of CHIPS.slice(2)) counts[key] = items.filter(chipFilter(key)).length;
  }
  const current = extra || (onlyActive ? "active" : "all");
  box.innerHTML = CHIPS.map(([key, label]) => `<button type="button" class="chip upnext-chip" data-action="upnext-chip" data-chip="${esc(key)}" aria-pressed="${key === current}"><span class="chip-l">${esc(label)}</span>${counts[key] != null ? ` <span class="chip-count num">${counts[key]}</span>` : ""}</button>`).join("");
}

async function fetchUpNext() {
  const container = document.getElementById("upnext-container");
  if (!container) return;
  showLoading(container, "Loading your next episodes…");
  try {
    const items = await api(`/api/up-next${onlyActive ? "?only_active_services=true" : ""}`);
    renderChips(items);
    renderUpNext(extra ? items.filter(chipFilter(extra)) : items, items.length);
  } catch (err) {
    container.innerHTML = `<div class="empty st st-crit">${icon("warn")}<span>${esc(err.message)}</span></div>`;
  }
}

function card(s) {
  const nxt = s.up_next;
  const caughtUp = s.episodes_left <= 0;
  let epLine;
  if (nxt && !caughtUp) {
    epLine = `<span class="upnext-code num">${esc(nxt.code)}</span> <span class="upnext-ep-title">${esc(nxt.title || "")}</span>`;
  } else if (s.next_episode_air_date) {
    epLine = `<span class="muted">Caught up. Next episode airs ${esc(s.next_episode_air_date)}</span>`;
  } else {
    epLine = `<span class="muted">Caught up</span>`;
  }
  const svc = s.service_name
    ? `<span class="service-tag ${s.service_active ? "svc-on" : "svc-off"}" title="${s.service_active ? "Subscribed now" : "Not subscribed right now"}">${esc(s.service_name)}</span>`
    : "";
  const pct = s.aired_episodes ? Math.round((s.watched_episodes / s.aired_episodes) * 100) : 0;
  const code = esc(nxt ? nxt.code : "next episode");
  return `
      <div class="upnext-card ${caughtUp ? "caught-up" : ""}">
        <img class="poster upnext-poster clickable" src="${esc(s.poster || PLACEHOLDER_POSTER)}" alt="" loading="lazy" data-fallback="poster" data-call="openShow" data-args='[${s.id}]' title="Details">
        <div class="upnext-info">
          <div class="upnext-title"><a class="title-link" href="#" data-call="openShow" data-prevent="1" data-args='[${s.id}]'>${esc(displayTitle(s))}</a> ${svc}</div>
          ${subtitleHtml(s)}
          <div class="upnext-ep">${epLine}</div>
          <div class="progress"><i data-w="${pct}"></i></div>
          <div class="upnext-meta num">${s.watched_episodes}/${s.aired_episodes || "?"} watched · ${s.episodes_left} left${householdShared ? ` · ${esc(householdText(s.household))}` : ""}</div>
        </div>
        <div class="upnext-actions">
          ${caughtUp ? "" : `<button class="btn outline watched-btn" data-call="markNext" data-args='[${s.id}]' title="Mark ${code} watched" aria-label="Mark ${code} watched">${icon("check")}<span class="watched-lbl">Watched</span></button>`}
          <select class="input sm" data-call="changeShowStatus" data-on="change" data-args='[${s.id}, "$value"]' title="Status${esc(syncsTo())}" aria-label="Status of ${esc(displayTitle(s))}${esc(syncsTo())}">${statusOptionsFor(s)}</select>
          <button class="btn sm details-btn" data-call="openShow" data-args='[${s.id}]'>${icon("info")}Details</button>
        </div>
      </div>`;
}

function renderUpNext(items, total) {
  const container = document.getElementById("upnext-container");
  if (!items.length && total) {
    container.innerHTML = `<div class="empty">${icon("search")}<span>Nothing matches this filter.</span></div>`;
    return;
  }
  if (!items.length) {
    container.innerHTML = `<div class="empty">${icon("queue")}<span><b>Nothing in progress</b>Start something from the Library or Discover tab.</span></div>`;
    return;
  }
  container.innerHTML = items.map(card).join("");
  setWidths(container);
}

async function markNext(showId) {
  try {
    const updated = await api(`/api/shows/${showId}/increment`, { method: "POST" });
    showToast(`${displayTitle(updated)}: ${updated.last_watched || updated.watched_episodes + " watched"}`);
    afterShowChange(updated);
  } catch (err) {
    showToast(err.message, true);
  }
}

function renderStats(stats) {
  const strip = document.getElementById("upnext-stats");
  if (!strip) return;
  strip.innerHTML = `
    <span class="stat-chip">${icon("tv")}<b class="num">${stats.watching_count}</b> watching</span>
    <span class="stat-chip">${icon("clock")}<b class="num">${Math.round(stats.total_backlog_hours)}</b> h backlog</span>
    <span class="stat-chip">${icon("check")}<b class="num">${stats.episodes_watched_30d}</b> episodes in 30 days</span>`;
}

// The Library's tidy-up check found finished shows with something new: say so here, with a way to it.
function renderTidyBanner({ returning, available }) {
  const banner = document.getElementById("tidy-banner");
  if (!banner) return;
  banner.hidden = !returning;
  if (!returning) return;
  banner.innerHTML = `${icon("sparkle")}<span class="grow"><b>${returning} show${returning === 1 ? "" : "s"} you finished have new episodes, seasons or sequels</b>${available ? ` (${available} out now)` : " coming"}.</span>
    <button class="btn sm outline" data-call="openTidy" data-args='[]'>Review</button>`;
}

register({ markNext });
registerAction("upnext-chip", (el) => setChip(el.dataset.chip));
onTabActivated(TAB, () => {
  fetchUpNext();
  if (statsStale) { statsStale = false; loadStats(); }
});
registerLoader(fetchUpNext);
on("stats", renderStats);
on("tidy-counts", renderTidyBanner);
on("subscriptions-changed", fetchUpNext);
// After a change: refreshed right away when this tab is open; the stats strip is refetched when it is opened
on("changed-soon", () => {
  if (state.activeTab === TAB) { fetchUpNext(); loadStats(); statsStale = false; } else statsStale = true;
});

export default {
  id: "upnext",
  mount() {
    // Keep Up Next fresh when the page is left open on a TV or tablet
    setInterval(() => { if (document.visibilityState === "visible" && state.activeTab === TAB) fetchUpNext(); }, 60000);
  },
};
