// Optimizer tab: subscription value report and rotation plan. The plan also feeds the header's "unused" figure
// ("plan" on the bus) and the planner (state.optimizerPlan).
import { api, showLoadError, showLoading } from "../../js/api.js";
import { emit, on } from "../../js/bus.js";
import { registerLoader } from "../../js/data.js";
import { registerAction } from "../../js/dispatch.js";
import { state } from "../../js/state.js";
import { onTabActivated } from "../../js/tabs.js";
import { PLACEHOLDER_POSTER, esc, householdShared, icon } from "../../js/util.js";
import "./planner.js";

const TAB = "optimizer-tab";
const money = (n) => `$${Number(n || 0).toFixed(2)}`;

async function fetchOptimizerPlan() {
  showLoading(document.getElementById("optimizer-rankings-container"), "Working out your subscriptions…");
  try {
    const plan = await api("/api/optimizer/plan");
    state.optimizerPlan = plan;
    // The header pill and the panel figure are both filled from here, so every plan refresh keeps them current.
    // What cancelling would really save; the raw "no viewing" figure only when the plan has no status data.
    const unusedValue = plan.savable_monthly_cost !== undefined ? plan.savable_monthly_cost : plan.unused_monthly_cost;
    document.getElementById("opt-unused").textContent = money(unusedValue);
    emit("plan", plan);
    renderOptimizerPlan(plan);
  } catch (err) {
    console.error("Error fetching optimizer plan:", err);
    showLoadError(document.getElementById("optimizer-rankings-container"), err);
  }
}

function renderStats(stats) {
  document.getElementById("opt-active-count").textContent = stats.active_services_count;
  document.getElementById("opt-monthly-spend").textContent = money(stats.current_monthly_spend);
}

// --- Optimizer ---

// A service's titles are in the Library: the Library tab shows them (it listens for this and opens itself).
function viewServiceInWatchlist(serviceId) {
  emit("show-service-titles", serviceId);
}

function badge(chip, title = "") {
  return `<span class="badge ${chip.cls}"${title ? ` title="${esc(title)}"` : ""}>${icon(chip.icon)} ${chip.label}</span>`;
}

const VERDICTS = {
  unused: { icon: "close", label: "Unused", cls: "st-crit" },
  finish_before_renewal: { icon: "clock", label: "Cancel before renewal", cls: "st-warn" },
  long_tail: { icon: "refresh", label: "Long tail: pause between binges", cls: "st-warn" },
  in_use: { icon: "ok", label: "In use", cls: "st-good" },
  covered: { icon: "ok", label: "Covered", cls: "st-good" },
};

function valueLine(rec, windowDays) {
  const v = VERDICTS[rec.verdict];
  const bits = [];
  if (rec.is_active || rec.hours_watched_window > 0) bits.push(`${rec.hours_watched_window}h watched in ${windowDays}d`);
  if (rec.cost_per_hour != null) bits.push(`$${rec.cost_per_hour.toFixed(2)}/h`);
  if (rec.days_to_clear != null) bits.push(`~${rec.days_to_clear}d to clear at your pace`);
  if (rec.renewal_in_days != null) bits.push(`renews in ${rec.renewal_in_days}d`);
  if (!v && !bits.length) return "";
  return `<div class="service-value">
    ${v ? badge(v, rec.verdict_text || "") : ""}
    <span class="value-bits">${bits.map(esc).join(" · ")}</span>
  </div>`;
}

function renderValueReport(plan) {
  const box = document.getElementById("value-report");
  if (!box) return;
  const rows = plan.service_rankings.filter((r) => r.is_active);
  if (!rows.length) { box.innerHTML = ""; return; }
  const maxCph = Math.max(0.01, ...rows.map((r) => r.cost_per_hour || 0));
  const order = { unused: 0, finish_before_renewal: 1, long_tail: 2, in_use: 3, covered: 4 };
  rows.sort((a, b) => (order[a.verdict] ?? 9) - (order[b.verdict] ?? 9) || (b.cost_per_hour || 0) - (a.cost_per_hour || 0));
  const unused = plan.unused_monthly_cost || 0;
  box.innerHTML = `
    <div class="value-head">
      <div class="stat value-kpi ${unused > 0 ? "st-warn" : "st-good"}">
        <span class="lbl">${icon(unused > 0 ? "warn" : "ok", "sm")}${unused > 0 ? `Paid for but not watched in ${plan.value_window_days} days` : "Every active subscription was used"}</span>
        <span class="val">${money(unused)}<small>/mo</small></span>
        <span class="sub">${unused > 0 ? `$${(unused * 12).toFixed(0)} a year` : ""}</span>
      </div>
      <div class="stat value-kpi">
        <span class="lbl">Household viewing pace, last ${plan.value_window_days} days</span>
        <span class="val">${plan.household_hours_per_day.toFixed(1)}<small>h/day</small></span>
      </div>
    </div>
    <div class="table-wrap">
      <table class="tbl value-table">
        <caption class="sr-only">Value of each active subscription over the last ${plan.value_window_days} days</caption>
        <thead><tr><th>Service</th><th class="num">$/mo</th><th class="num">Watched</th><th>Cost per hour</th><th class="num">Backlog</th><th class="num">Days to clear</th><th class="num">Renews</th><th>Verdict</th></tr></thead>
        <tbody>${rows.map((r) => {
          const v = VERDICTS[r.verdict] || { icon: "unknown", label: "Unknown", cls: "st-unknown" };
          const w = r.cost_per_hour ? Math.max(4, Math.round((r.cost_per_hour / maxCph) * 100)) : 0;
          return `<tr>
            <th scope="row">${esc(r.service_name)}</th>
            <td class="num">${money(r.monthly_cost)}</td>
            <td class="num">${r.hours_watched_window}h</td>
            <td><div class="bar"><div class="bar-track"><div class="bar-fill" data-w="${w}"></div></div><span>${r.cost_per_hour != null ? "$" + r.cost_per_hour.toFixed(2) + "/h" : "n/a"}</span></div></td>
            <td class="num">${r.hours_backlog}h</td>
            <td class="num">${r.days_to_clear != null ? r.days_to_clear : "—"}</td>
            <td class="num">${r.renewal_in_days != null ? r.renewal_in_days + "d" : "—"}</td>
            <td>${badge(v, r.verdict_text || "")}</td>
          </tr>`;
        }).join("")}</tbody>
      </table>
    </div>`;
  box.querySelectorAll("[data-w]").forEach((el) => { el.style.width = `${el.dataset.w}%`; });
}

function renderBundleSuggestions(plan) {
  const box = document.getElementById("bundle-suggestions");
  if (!box) return;
  const items = plan.bundle_suggestions || [];
  if (!items.length) { box.innerHTML = ""; return; }
  box.innerHTML = `
    <div class="bundle-card">
      <h3 class="section-t">${icon("gift")}Bundle suggestions</h3>
      <ul class="bundle-list">${items
        .map(
          (s) => `<li class="bundle-item ${s.kind === "bundle_not_worth_it" ? "st-warn" : "st-good"}">
            <div class="bundle-text">${esc(s.text)}</div>
            <div class="bundle-meta">
              <span class="bundle-saving num">${money(s.monthly_saving)}/mo</span>
              <a href="#/services" class="bundle-link">Go to Services</a>
            </div>
          </li>`
        )
        .join("")}</ul>
    </div>`;
}

// A bundle member is billed through its bundle: say so instead of "$0.00/month" and offer no toggle.
function priceLine(rec, plan) {
  if (rec.is_free) return "Included (no extra cost)";
  if (!rec.covered_by) return `${money(rec.monthly_cost)}/month`;
  const bundle = plan.service_rankings.find((r) => r.service_id === rec.covered_by);
  return `Included in ${esc(bundle ? bundle.service_name : rec.covered_by)}`;
}

// Nothing to cancel on a service that costs nothing extra (or is billed through a bundle).
function toggleButton(rec) {
  if (rec.covered_by) return "";
  if (rec.is_free) return "";
  return `<button class="btn sm outline" data-action="toggle-service" data-service="${esc(rec.service_id)}" data-active="${!rec.is_active}">
          ${rec.is_active ? "Mark cancelled" : "Activate subscription"}
        </button>`;
}

// --- One status per service (plan.service_rankings[].status, computed by the server) ---

// Most urgent first. Inactive services are not listed here: they appear under "What to subscribe to next".
export const STATUS_ORDER = ["consider_cancel", "decide_soon", "check", "watching", "in_use", "paid_through", "included", "covered"];

const STATUS_CHIPS = {
  consider_cancel: { icon: "close", label: "Consider cancelling", cls: "st-crit" },
  decide_soon: { icon: "clock", label: "Decide soon", cls: "st-warn" },
  check: { icon: "unknown", label: "Check", cls: "st-warn" },
  watching: { icon: "play", label: "Watching", cls: "st-good" },
  in_use: { icon: "ok", label: "In use", cls: "st-good" },
  paid_through: { icon: "ok", label: "Paid through", cls: "st-good" },
  included: { icon: "ok", label: "Included", cls: "st-good" },
  covered: { icon: "ok", label: "Covered", cls: "st-good" },
};

// Active services in status order (most urgent first), then by name.
export function sortByStatus(recs) {
  const rank = (r) => { const i = STATUS_ORDER.indexOf(r.status); return i < 0 ? STATUS_ORDER.length : i; };
  return recs
    .filter((r) => STATUS_ORDER.includes(r.status))
    .sort((a, b) => rank(a) - rank(b) || a.service_name.localeCompare(b.service_name));
}

function costCell(rec, plan) {
  if (rec.is_free || rec.covered_by) return priceLine(rec, plan);
  if (rec.billing_period === "yearly") return `$${(rec.monthly_cost * 12).toFixed(2)}/yr`;
  return `${money(rec.monthly_cost)}/mo`;
}

function renderSummary(plan) {
  const box = document.getElementById("opt-summary");
  if (!box) return;
  const nd = plan.next_decision;
  const decision = nd
    ? `${esc(nd.service_name)}, ${esc(nd.date)} (${esc(nd.days)} ${nd.days === 1 ? "day" : "days"})`
    : "No decision needed in the next 45 days";
  const savable = plan.savable_monthly_cost || 0;
  box.innerHTML = `
    <div class="panel opt-sum-card"><div class="stat"><span class="lbl">You pay</span><span class="val opt-sum-num">${money(plan.current_monthly_spend)}<small>/mo</small></span></div></div>
    <div class="panel opt-sum-card ${savable > 0 ? "opt-sum-save st-warn" : ""}"${savable > 0 ? ' data-status="warn"' : ""}><div class="stat"><span class="lbl">Could save</span>
      <span class="val opt-sum-num">${money(savable)}<small>/mo</small></span>
      <span class="sub">only what is cancellable</span></div></div>
    <div class="panel opt-sum-card"><div class="stat"><span class="lbl">Next decision</span><span class="opt-sum-text">${decision}</span></div></div>`;
}

function subscriptionRow(rec, plan) {
  const chip = STATUS_CHIPS[rec.status];
  const actionable = rec.status === "consider_cancel" || rec.status === "decide_soon";
  return `<tr class="opt-row opt-${esc(rec.status)}">
    <th scope="row">${esc(rec.service_name)}</th>
    <td class="num">${costCell(rec, plan)}</td>
    <td><span class="badge ${chip.cls}">${icon(chip.icon)} ${chip.label}</span></td>
    <td class="opt-why">${esc(rec.status_text || "")}</td>
    <td class="opt-actions"><button class="btn sm" data-action="view-service" data-service="${esc(rec.service_id)}">${icon("library")}View</button>
      ${actionable ? toggleButton(rec) : ""}</td>
  </tr>`;
}

function renderSubscriptions(plan) {
  const box = document.getElementById("opt-subs-table");
  if (!box) return;
  const rows = sortByStatus(plan.service_rankings.slice());
  if (!rows.length) {
    box.innerHTML = `<div class="empty opt-empty">${icon("card")}<span>No active subscriptions yet. Activate one on the Services tab.</span></div>`;
    return;
  }
  box.innerHTML = `<div class="table-wrap"><table class="tbl opt-table">
    <caption class="sr-only">Every active subscription with what to do about it</caption>
    <thead><tr><th>Service</th><th class="num">Cost</th><th>Status</th><th>Why</th><th></th></tr></thead>
    <tbody>${rows.map((r) => subscriptionRow(r, plan)).join("")}</tbody></table></div>`;
}

function actionBadge(rec) {
  if (rec.is_active) {
    return rec.action === "cancel_recommended"
      ? { cls: "st-crit", icon: "close", label: "Ready to cancel" }
      : { cls: "st-good", icon: "ok", label: "Active" };
  }
  if (rec.action === "activate_now") return { cls: "st-good", icon: "play", label: "Recommended next" };
  if (rec.action === "wait_for_finale") return { cls: "st-busy", icon: "clock", label: "Wait for finale" };
  return { cls: "st-unknown", icon: "pause", label: "In queue" };
}

function rankingCard(rec, plan) {
  const card = document.createElement("div");
  card.className = `panel service-card${rec.action === "activate_now" ? " recommend-activate" : ""}`;
  let showsPreviewHtml = "";
  if (rec.top_shows && rec.top_shows.length > 0) {
    showsPreviewHtml = `
      <button type="button" class="queued-shows-preview" data-action="view-service" data-service="${esc(rec.service_id)}" title="View all ${esc(rec.service_name)} titles">
        <span class="preview-title"><span>Queued (${rec.shows_count})</span><span class="preview-all">View all${icon("right", "sm")}</span></span>
        <span class="preview-shows-list">
          ${rec.top_shows
            .map((s) => `<img class="poster preview-item" src="${esc(s.poster || PLACEHOLDER_POSTER)}" title="${esc(s.title)} (IMDb ${s.imdb_rating || "N/A"})" alt="" data-fallback="poster">`)
            .join("")}
        </span>
      </button>`;
  }
  card.innerHTML = `
    <div class="card-top">
      <button type="button" class="service-head" data-action="view-service" data-service="${esc(rec.service_id)}">
        <span class="service-name">${esc(rec.service_name)}</span>
        <span class="service-price">${priceLine(rec, plan)}</span>
      </button>
      ${badge(actionBadge(rec))}
    </div>
    <div class="service-metrics">
      <div class="stat"><span class="val">${rec.hours_backlog}<small>h</small></span><span class="lbl">Backlog</span></div>
      <div class="stat"><span class="val">${rec.ready_to_binge_count}</span><span class="lbl">Ready to binge</span></div>
      ${householdShared ? `<div class="stat"><span class="val">${rec.both_want_count}</span><span class="lbl">Both want</span></div>` : ""}
      <div class="stat"><span class="val">${rec.avg_imdb_rating || "—"}</span><span class="lbl">Avg IMDb</span></div>
    </div>
    <div class="service-reason">${esc(rec.recommendation_reason)}</div>
    ${valueLine(rec, plan.value_window_days)}
    ${showsPreviewHtml}
    <div class="card-actions-row">
      <button class="btn sm" data-action="view-service" data-service="${esc(rec.service_id)}">${icon("library")}View (${rec.shows_count})</button>
      ${toggleButton(rec)}
    </div>`;
  return card;
}

function renderOptimizerPlan(plan) {
  renderValueReport(plan);
  renderBundleSuggestions(plan);
  renderSummary(plan);
  renderSubscriptions(plan);
  const adviceBox = document.getElementById("rotation-advice-text");
  if (plan.rotation_advice && plan.rotation_advice.length > 0) {
    adviceBox.innerHTML = `<ul>${plan.rotation_advice.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>`;
  } else {
    adviceBox.textContent = "Your streaming subscription rotation is running efficiently!";
  }

  const container = document.getElementById("optimizer-rankings-container");
  container.innerHTML = "";
  plan.service_rankings.filter((rec) => !rec.is_active && !rec.covered_by).forEach((rec) => container.appendChild(rankingCard(rec, plan)));
  if (!container.children.length) container.innerHTML = `<div class="empty">${icon("ok")}<span>Every service you track is subscribed.</span></div>`;
}

registerAction("view-service", (el) => viewServiceInWatchlist(el.dataset.service));
onTabActivated(TAB, () => fetchOptimizerPlan());
registerLoader(fetchOptimizerPlan);
on("stats", renderStats);
on("changed-later", fetchOptimizerPlan);
on("subscriptions-changed", fetchOptimizerPlan);
on("prices-changed", fetchOptimizerPlan);

export default { id: "optimizer", mount() {} };
