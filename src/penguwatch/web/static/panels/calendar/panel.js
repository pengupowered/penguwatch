// Schedule tab: upcoming episodes calendar and iCal link.
import { api } from "../../js/api.js";
import { registerLoader } from "../../js/data.js";
import { onTabActivated } from "../../js/tabs.js";
import { PLACEHOLDER_POSTER, displayTitle, esc, icon, showToast, subtitleHtml } from "../../js/util.js";

let opened = false; // the schedule is fetched when the tab is first opened, not with the page

async function fetchUpcomingCalendar() {
  opened = true;
  try {
    renderCalendar(await api("/api/calendar/upcoming?days_ahead=45"));
  } catch (err) {
    console.error("Error fetching calendar:", err);
  }
}

// --- Calendar ---

async function setupCalendarExtras() {
  let path = "/api/calendar.ics";
  try { path = (await api("/api/system")).calendar_feed || path; } catch { /* default path */ }
  const url = `${window.location.origin}${path}`;
  document.getElementById("ical-url").textContent = url;
  document.getElementById("btn-copy-ical").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(url);
      showToast("Calendar link copied");
    } catch {
      showToast(url);
    }
  });
}

function renderCalendar(items) {
  const container = document.getElementById("calendar-timeline");
  container.innerHTML = "";
  if (!items || items.length === 0) {
    container.innerHTML = `<div class="empty">${icon("calendar")}<span>No upcoming episodes in the next 45 days for your tracked shows.</span></div>`;
    return;
  }
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  items.forEach((item) => {
    const d = new Date(String(item.next_episode_air_date).slice(0, 10) + "T00:00:00");
    const diffDays = Math.round((d - today) / 86400000);
    let countdown;
    if (diffDays === 0) countdown = ["st-good", "play", "Today"];
    else if (diffDays === 1) countdown = ["st-busy", "clock", "Tomorrow"];
    else if (diffDays === -1) countdown = ["st-unknown", "check", "Yesterday"];
    else if (diffDays < -1) countdown = ["st-unknown", "check", "Just aired"];
    else countdown = ["", "calendar", `In ${diffDays} days`];
    const svc = item.service_name || item.network || "";
    const card = document.createElement("button");
    card.type = "button";
    card.className = "timeline-card clickable";
    card.dataset.call = "openShow";
    card.dataset.args = JSON.stringify([item.id]);
    card.innerHTML = `
      <span class="timeline-date-col">
        <span class="tl-month">${d.toLocaleString("default", { month: "short" })}</span>
        <span class="tl-day">${d.getDate()}</span>
      </span>
      <img class="poster timeline-thumb" src="${esc(item.poster || PLACEHOLDER_POSTER)}" alt="" loading="lazy" data-fallback="poster">
      <span class="timeline-info">
        <span class="tl-title">${esc(displayTitle(item))}</span>
        ${subtitleHtml(item, "span")}
        <span class="tl-ep-info">
          <span class="num">S${item.next_episode_season || 1}E${item.next_episode_number || 1}: ${esc(item.next_episode_title || "New Episode")}</span>
          ${svc ? `<span class="service-tag ${item.is_service_active ? "svc-on" : ""}">${esc(svc)}</span>` : ""}
        </span>
      </span>
      <span class="tl-countdown badge quiet ${countdown[0]}">${icon(countdown[1])}${countdown[2]}</span>`;
    container.appendChild(card);
  });
}

onTabActivated("calendar-tab", () => fetchUpcomingCalendar());
// after a sync or a change the schedule is refreshed only if the tab has been opened before; nothing else needs it at load
registerLoader(() => (opened ? fetchUpcomingCalendar() : undefined));

export default { id: "calendar", mount: setupCalendarExtras };
