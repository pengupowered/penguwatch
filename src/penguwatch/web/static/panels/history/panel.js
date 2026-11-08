// History tab: what was watched over the last 7, 30 or 365 days, against the period before.
// The numbers all come from /api/history (rolling local days; bulk marks left out unless asked for). The DOM carries
// stable ids and data attributes: #history-period buttons[data-days], #history-totals [data-metric], and one element
// per section (#history-series, -shows, -services, -genres, -weekdays, -hours, -bulk, -empty).
import { api } from "../../js/api.js";
import { onTabActivated } from "../../js/tabs.js";
import { PLACEHOLDER_POSTER, esc, icon, setWidths } from "../../js/util.js";
import { barList, columnChart, fmt, table } from "./charts.js";

const STORE = "history-days";
const PERIODS = [7, 30, 365];
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]; // index 0 is Sunday, as on the Insights tab
const WORDS = { 7: "7 days", 30: "30 days", 365: "12 months" };

let days = 30;
let includeBulk = false;
try { const saved = Number(localStorage.getItem(STORE)); if (PERIODS.includes(saved)) days = saved; } catch { /* private mode */ }

// "+12%" as an arrow and a figure; "new" when there was nothing before; nothing when both are zero
function change(cur, prev) {
  if (!cur && !prev) return { text: "", dir: "none" };
  if (!prev) return { text: "new", dir: "new" };
  const pct = Math.round(((cur - prev) / prev) * 100);
  if (pct === 0) return { text: "no change", dir: "same" };
  return { text: `${Math.abs(pct)}%`, dir: pct > 0 ? "up" : "down" };
}

function tile(metric, label, value, unit, cur, prev, extra = "") {
  const c = change(cur, prev);
  const arrow = c.dir === "up" ? icon("arrow-up", "sm") : c.dir === "down" ? icon("arrow-down", "sm") : "";
  const sub = c.dir === "none" ? "" : `${arrow} ${c.text} <span class="hist-vs">vs ${fmt(prev)}${unit ? ` ${unit}` : ""} before</span>`;
  return `<div class="panel tile" data-metric="${metric}"><div class="stat"><span class="lbl">${esc(label)}</span>
    <span class="val" data-role="value">${value}${unit ? `<small>${unit}</small>` : ""}</span>
    <span class="sub hist-change" data-role="change" data-dir="${c.dir}">${sub}</span>${extra}</div></div>`;
}

const card = (id, title, sub, body, ic) => `
  <div class="panel" id="${id}">
    <div class="panel-h">${icon(ic)}<h2 class="panel-t">${esc(title)}</h2><span class="panel-m">${esc(sub)}</span></div>
    <div class="panel-b">${body}</div>
  </div>`;

const money = (n) => `$${n.toFixed(2)}`;

function render(d) {
  const cur = d.totals.current, prev = d.totals.previous;
  const root = document.getElementById("history-container");
  const W = root.clientWidth > 300 ? Math.round(root.clientWidth - 2 * 17) : 360; // the panel body's inner width
  const word = WORDS[d.period.days];
  const items = cur.episodes + cur.movies;
  root.dataset.days = String(d.period.days); // what is drawn now (the tests wait on these)
  root.dataset.bulk = String(d.include_bulk);

  const bulk = d.bulk_episodes
    ? `<p class="muted hist-bulk" id="history-bulk" data-included="${d.include_bulk}">${d.include_bulk
      ? `${d.bulk_episodes} episodes marked in bulk are counted as watching time. <button type="button" class="btn-link" id="history-bulk-toggle">Leave them out</button>`
      : `${d.bulk_episodes} episodes marked in bulk aren't counted as watching time. <button type="button" class="btn-link" id="history-bulk-toggle">Include them</button>`}</p>`
    : "";

  if (!items) {
    root.innerHTML = `<div class="empty" id="history-empty">${icon("history")}<span><b>Nothing watched in the last ${word}</b>Episodes and movies you mark as watched show up here.</span></div>${bulk}`;
    return;
  }

  const monthly = d.series.unit === "month";
  const buckets = d.series.buckets;
  const services = d.by_service.filter((s) => s.hours > 0);
  const hasCost = d.by_service.some((s) => s.cost != null);
  const dayMax = Math.max(...d.weekdays, 0);
  const hourMax = Math.max(...d.hours_of_day, 0);
  const timed = d.hours_of_day.reduce((a, b) => a + b, 0);
  const dateOf = (b) => new Date(`${b.start}T12:00:00`);
  const longLabel = (b) => monthly
    ? dateOf(b).toLocaleDateString(undefined, { month: "long", year: "numeric" }) + (b.partial ? " (part of the month)" : "")
    : dateOf(b).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });

  root.innerHTML = `
    <div class="tiles" id="history-totals">
      ${tile("hours", "Hours watched", fmt(cur.hours), "h", cur.hours, prev.hours)}
      ${tile("episodes", "Episodes", cur.episodes.toLocaleString(), "", cur.episodes, prev.episodes)}
      ${tile("movies", "Movies", cur.movies.toLocaleString(), "", cur.movies, prev.movies)}
      ${tile("titles_started", "Titles started", cur.titles_started.toLocaleString(), "", cur.titles_started, prev.titles_started)}
      ${tile("titles_finished", "Titles finished", cur.titles_finished.toLocaleString(), "", cur.titles_finished, prev.titles_finished)}
      ${tile("active_days", "Days with viewing", cur.active_days.toLocaleString(), "", cur.active_days, prev.active_days)}
    </div>
    ${bulk}

    ${card("history-series", monthly ? "Hours per month" : "Hours per day", monthly ? "the first and last month are partial (lighter bars)" : `the last ${word}`,
      columnChart(buckets, {
        label: (b) => ({ short: monthly ? b.label : String(Number(b.start.slice(8))), long: longLabel(b) }),
        value: (b) => b.hours, unit: "h", W, labelEvery: monthly ? 1 : (d.period.days <= 7 ? 1 : 5),
        partial: (b) => b.partial,
      })
      + table([monthly ? "Month" : "Day", "Hours", "Episodes", "Movies"], buckets.map((b) => [monthly ? b.key : b.start, fmt(b.hours), b.episodes, b.movies])), "chart")}

    <div class="hist-two">
      ${card("history-shows", "Top shows", "by hours in this period",
        d.top_shows.length ? `<ol class="hist-shows">${d.top_shows.map((s) => `
          <li class="hist-show" data-show-id="${s.id}">
            <img class="poster hist-poster" src="${esc(s.poster || PLACEHOLDER_POSTER)}" alt="" loading="lazy" data-fallback="poster">
            <div class="hist-show-b"><div class="hist-show-t">${esc(s.title)}</div>
              <div class="bar-track"><div class="bar-fill" data-w="${Math.max(2, (s.hours / d.top_shows[0].hours) * 100)}"></div></div></div>
            <div class="hl-val">${fmt(s.hours)} h<span class="hist-sub">${s.episodes} ${s.episodes === 1 ? "watch" : "watches"}</span></div>
          </li>`).join("")}</ol>` : '<p class="muted">Nothing counted.</p>', "tv")}
      ${card("history-services", "Hours by service", "current service assignment",
        (services.length ? barList(services.map((s) => ({ name: s.service, value: s.hours,
          sub: s.cost_per_hour != null ? `${money(s.cost_per_hour)} per hour` : (s.cost != null ? `${money(s.cost)}, no viewing` : "") })), "h")
          : '<p class="muted">No viewing in this period.</p>')
        + (hasCost ? `<p class="muted hist-note" id="history-cost-note">Cost per hour takes each service that is active now as paid for the whole period, at the price in effect each day (PenguWatch keeps no record of past subscriptions), divided by the hours watched on it. Services you have cancelled show hours only.</p>` : "")
        + table(["Service", "Hours", "Episodes and movies", "Cost", "Cost per hour"],
          d.by_service.map((s) => [s.service, fmt(s.hours), s.episodes, s.cost == null ? "–" : money(s.cost), s.cost_per_hour == null ? "–" : money(s.cost_per_hour)])), "card")}
    </div>

    ${card("history-genres", "Top genres", "hours; a show counts toward every genre it has",
      d.genres.length ? barList(d.genres.map((g) => ({ name: g.genre, value: g.hours })), "h") + table(["Genre", "Hours"], d.genres.map((g) => [g.genre, fmt(g.hours)]))
        : '<p class="muted">No genre data.</p>', "sparkle")}

    <div class="hist-two">
      ${card("history-weekdays", "Weekdays", "episodes and movies per day of the week",
        columnChart(DAYS.map((n, i) => ({ n, v: d.weekdays[i] })), { label: (x) => ({ short: x.n, long: x.n }), value: (x) => x.v, unit: "watches", height: 150, W: Math.round(W / (W > 640 ? 2 : 1)) })
        + (dayMax ? `<p class="muted hist-note">Busiest: ${DAYS[d.weekdays.indexOf(dayMax)]}.</p>` : ""), "calendar")}
      ${card("history-hours", "Time of day", timed ? `when you watched, ${d.period.tz} time` : "no times recorded",
        timed ? columnChart(d.hours_of_day.map((v, h) => ({ h, v })), { label: (x) => ({ short: String(x.h), long: `${x.h}:00` }), value: (x) => x.v, unit: "watches", height: 150, W: Math.round(W / (W > 640 ? 2 : 1)), labelEvery: 3 })
          + `<p class="muted hist-note">Peak: ${hourMax ? `${d.hours_of_day.indexOf(hourMax)}:00` : "–"}. Only watches with a recorded time are shown.</p>`
          : '<p class="muted">The watches in this period have no time of day, only a date.</p>', "clock")}
    </div>`;
  setWidths(root);
}

async function load() {
  const root = document.getElementById("history-container");
  if (!root) return;
  document.querySelectorAll("#history-period button").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.days) === days)));
  try {
    render(await api(`/api/history?days=${days}&include_bulk=${includeBulk}`));
  } catch (e) {
    root.innerHTML = `<div class="empty st st-crit">${icon("warn")}<span><b>Couldn't load history</b>${esc(e.message)}</span></div>`;
  }
}

onTabActivated("history-tab", load);

export default {
  id: "history",
  mount() {
    document.getElementById("history-period")?.addEventListener("click", (ev) => {
      const b = ev.target.closest("button[data-days]");
      if (!b) return;
      days = Number(b.dataset.days);
      try { localStorage.setItem(STORE, String(days)); } catch { /* private mode */ }
      load();
    });
    document.getElementById("history-container")?.addEventListener("click", (ev) => {
      if (!ev.target.closest("#history-bulk-toggle")) return;
      includeBulk = !includeBulk;
      load();
    });
  },
};
