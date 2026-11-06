// Insights tab: monthly viewing, hours per service, top genres, weekday rhythm.
// One hue throughout (these are all magnitudes of the same thing: hours or episodes); every chart has a data table
// behind a <details> for screen readers and copy/paste. The SVG carries classes only: panel.css colours it from the
// tokens, so it follows the theme.
import { api } from "../../js/api.js";
import { onTabActivated } from "../../js/tabs.js";
import { esc, icon, setWidths } from "../../js/util.js";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const fmt = (n) => (n >= 100 ? Math.round(n).toLocaleString() : (Math.round(n * 10) / 10).toString());

// `W` is the drawing's width in CSS pixels (the panel's), so its text stays at the type scale's size
function columnChart(items, { label, value, unit, height = 190, W = 760 }) {
  const padL = 36, padB = 26, padT = 14;
  const max = Math.max(1, ...items.map(value));
  const niceMax = Math.ceil(max / 10) * 10 || 10;
  const plotW = W - padL - 8, plotH = height - padB - padT;
  const slot = plotW / items.length, bw = Math.min(34, slot * 0.62);
  const y = (v) => padT + plotH - (v / niceMax) * plotH;
  const grid = [0, 0.5, 1].map((f) => {
    const v = niceMax * f;
    return `<line x1="${padL}" x2="${W - 8}" y1="${y(v)}" y2="${y(v)}" class="ch-grid"/><text x="${padL - 6}" y="${y(v) + 4}" class="ch-axis" text-anchor="end">${fmt(v)}</text>`;
  }).join("");
  const bars = items.map((it, i) => {
    const v = value(it), x = padL + slot * i + (slot - bw) / 2, h = Math.max(v > 0 ? 2 : 0, plotH - (y(v) - padT));
    const top = y(v);
    // flat baseline end, 4px rounded data end
    const r = Math.min(4, bw / 2, h);
    const path = `M${x},${padT + plotH} V${top + r} Q${x},${top} ${x + r},${top} H${x + bw - r} Q${x + bw},${top} ${x + bw},${top + r} V${padT + plotH} Z`;
    const lbl = label(it);
    const showLabel = items.length <= 12 || i % 2 === 0;
    return `<g><path d="${path}" class="ch-bar"><title>${esc(lbl.long)}: ${fmt(v)} ${unit}</title></path>
      ${showLabel ? `<text x="${x + bw / 2}" y="${height - 8}" class="ch-axis" text-anchor="middle">${esc(lbl.short)}</text>` : ""}</g>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${height}" class="ch-svg" role="img" aria-label="${unit} per period">${grid}${bars}</svg>`;
}

function hbars(rows, unit) {
  const max = Math.max(0.1, ...rows.map((r) => r.value));
  return `<div class="hb">${rows.map((r) => `
    <div class="hb-row" title="${esc(r.name)}: ${fmt(r.value)} ${unit}">
      <div class="hb-name">${esc(r.name)}</div>
      <div class="bar-track"><div class="bar-fill" data-w="${Math.max(2, (r.value / max) * 100)}"></div></div>
      <div class="hb-val">${fmt(r.value)} ${unit}</div>
    </div>`).join("")}</div>`;
}

const table = (head, rows) =>
  `<details class="ch-table"><summary>Show as table</summary><div class="table-wrap"><table class="tbl"><thead><tr>${head.map((h, i) => `<th class="${i ? "num" : ""}">${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c, i) => `<td class="${i ? "num" : ""}">${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div></details>`;

const card = (title, sub, body, ic) => `
  <div class="panel">
    <div class="panel-h">${icon(ic)}<h2 class="panel-t">${esc(title)}</h2><span class="panel-m">${esc(sub)}</span></div>
    <div class="panel-b">${body}</div>
  </div>`;

function render(d) {
  const months = d.months;
  const last = months[months.length - 1] || { hours: 0, episodes: 0 };
  const prev = months[months.length - 2] || { hours: 0 };
  const perMonth = months.length ? d.total_hours / months.length : 0;
  const trend = prev.hours > 0 ? Math.round(((last.hours - prev.hours) / prev.hours) * 100) : null;
  const services = d.by_service.filter((s) => s.hours > 0).slice(0, 8).map((s) => ({ name: s.service, value: s.hours }));
  const genres = d.genres.slice(0, 8).map((g) => ({ name: g.genre, value: g.hours }));
  const dayMax = Math.max(...d.weekdays, 0);
  const busiest = dayMax ? DAYS[d.weekdays.indexOf(dayMax)] : "—";
  const tile = (v, l, s) => `<div class="panel tile"><div class="stat"><span class="lbl">${esc(l)}</span><span class="val">${v}</span>${s ? `<span class="sub">${s}</span>` : ""}</div></div>`;
  const trendText = trend == null ? "" : `${icon(trend >= 0 ? "arrow-up" : "arrow-down", "sm")} ${Math.abs(trend)}% ${trend >= 0 ? "more" : "less"} than the previous month`;

  const root = document.getElementById("insights-container");
  const W = root.clientWidth > 360 ? Math.round(root.clientWidth - 2 * 17) : 760; // the panel body's inner width
  root.innerHTML = `
    <div class="tiles">
      ${tile(fmt(d.total_hours) + "<small>h</small>", `Watched in ${months.length} months`, `${d.total_episodes.toLocaleString()} episodes and movies`)}
      ${tile(fmt(perMonth) + "<small>h</small>", "Per month on average", trendText)}
      ${tile(d.active_days_90d, "Days with viewing, last 90", `${Math.round((d.active_days_90d / 90) * 100)}% of days`)}
      ${tile(d.completion_rate == null ? "—" : Math.round(d.completion_rate * 100) + "%", "Finish rate", "completed vs dropped")}
      ${tile(busiest, "Busiest weekday", dayMax ? `${dayMax} episodes` : "")}
    </div>

    ${card("Hours watched per month", "episodes and movies, using each title's runtime",
      columnChart(months, { label: (m) => { const [y, mo] = m.month.split("-"); return { short: MONTHS[+mo - 1], long: `${MONTHS[+mo - 1]} ${y}` }; }, value: (m) => m.hours, unit: "h", W })
      + table(["Month", "Hours", "Episodes"], months.map((m) => [m.month, fmt(m.hours), m.episodes])), "chart")}

    <div class="ins-two">
      ${card("Hours by service", "where the viewing happened (current service assignment)",
        (services.length ? hbars(services, "h") : '<p class="muted">No viewing in this period.</p>')
        + table(["Service", "Hours", "Episodes"], d.by_service.map((s) => [s.service, fmt(s.hours), s.episodes])), "card")}
      ${card("Top genres", "hours; a show counts toward every genre it has",
        (genres.length ? hbars(genres, "h") : '<p class="muted">No genre data.</p>')
        + table(["Genre", "Hours"], d.genres.map((g) => [g.genre, fmt(g.hours)])), "sparkle")}
    </div>

    ${card("Weekday rhythm", "episodes watched per day of the week (UTC dates)",
      columnChart(DAYS.map((n, i) => ({ n, v: d.weekdays[i] })), { label: (x) => ({ short: x.n, long: x.n }), value: (x) => x.v, unit: "episodes", height: 150, W }), "calendar")}`;
  setWidths(root);
}

async function loadInsights() {
  const root = document.getElementById("insights-container");
  if (!root) return;
  const months = document.getElementById("insights-months").value || 12;
  try {
    render(await api(`/api/insights?months=${encodeURIComponent(months)}`));
  } catch (e) {
    root.innerHTML = `<div class="empty st st-crit">${icon("warn")}<span><b>Couldn't load insights</b>${esc(e.message)}</span></div>`;
  }
}

onTabActivated("insights-tab", loadInsights);

export default {
  id: "insights",
  mount() {
    document.getElementById("insights-months")?.addEventListener("change", loadInsights);
  },
};
