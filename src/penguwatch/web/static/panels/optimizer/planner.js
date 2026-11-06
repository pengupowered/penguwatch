// Rotation planner on the Optimizer tab: summary cards, a month-by-service timeline and the same data as a table.
import { api } from "../../js/api.js";
import { register, registerChange } from "../../js/dispatch.js";
import { state } from "../../js/state.js";
import { onTabActivated } from "../../js/tabs.js";
import { displayTitle, esc, icon } from "../../js/util.js";

// Each kind of segment: its sprite icon and the word when the server sends no label
const KINDS = {
  keep: { icon: "check", fallback: "Keep" },
  subscribe: { icon: "play", fallback: "Subscribe" },
  cancel: { icon: "close", fallback: "Cancel before renewal" },
  pause: { icon: "pause", fallback: "Paused" },
  wait: { icon: "clock", fallback: "Waiting" },
};
const MOBILE = "(max-width: 720px)";
const MONTH_CHOICES = [3, 6, 12];

let months = 6;
let loadSeq = 0;

const $ = (id) => document.getElementById(id);
const money = (n) => `$${Number(n || 0).toFixed(2)}`;

function monthLabels(count) {
  const now = new Date();
  return Array.from({ length: count }, (_, i) => {
    const d = new Date(now.getFullYear(), now.getMonth() + i, 1);
    return d.toLocaleDateString(undefined, { month: "short" });
  });
}

function kindOf(seg) {
  return KINDS[seg.kind] || { icon: "minus", fallback: String(seg.kind) };
}

function segText(seg) {
  return seg.label || kindOf(seg).fallback;
}

function serviceNames(plan) {
  return Object.fromEntries(plan.services.map((s) => [s.service_id, s.name]));
}

function nameOf(names, id) {
  return names[id] || id;
}

function relationHtml(svc, names) {
  const out = [];
  if (svc.is_bundle && svc.includes.length) {
    out.push(`Includes: ${svc.includes.map((id) => nameOf(names, id)).join(", ")}`);
  }
  if (svc.covered_by) out.push(`Included in ${nameOf(names, svc.covered_by)}`);
  return out.map((l) => `<div class="planner-rel">${esc(l)}</div>`).join("");
}

// --- Summary cards ---

function renderSummary(plan) {
  const s = plan.summary;
  const diff = s.monthly_difference;
  const diffTxt = Math.abs(diff) < 0.005 ? "same as today"
    : `${money(Math.abs(diff))}/mo ${diff < 0 ? "cheaper" : "more"} than today`;
  const names = serviceNames(plan);
  const byId = Object.fromEntries(plan.services.map((x) => [x.service_id, x]));
  const cancel = s.cancel_now.map((id) => {
    const svc = byId[id];
    const price = svc && !svc.is_free ? ` <span class="planner-price">${money(svc.monthly_cost)}/mo</span>` : "";
    return `<li>${icon("close", "sm ink-crit")} ${esc(nameOf(names, id))}${price}</li>`;
  }).join("");
  const unused = state.optimizerPlan ? state.optimizerPlan.unused_monthly_cost : null;
  const known = unused !== null && unused !== undefined;
  $("planner-summary").innerHTML = `
    <div class="planner-card stat">
      <span class="lbl">Planned average over ${esc(plan.months)} months</span>
      <span class="val">${money(s.planned_avg_monthly_spend)}<small>/mo</small></span>
      <span class="sub">Today ${money(s.current_monthly_spend)}/mo; ${esc(diffTxt)}</span>
    </div>
    <div class="planner-card stat">
      <span class="lbl">Cancel now</span>
      <ul id="planner-cancel-now" class="planner-list">${cancel || "<li>Nothing to cancel</li>"}</ul>
    </div>
    <div class="planner-card stat">
      <span class="lbl">Paid for but unused</span>
      <span class="val" id="planner-unused">${known ? money(unused) : "n/a"}<small>${known ? "/mo" : ""}</small></span>
    </div>`;
}

// --- Timeline grid ---

function segCell(seg, count) {
  const k = kindOf(seg);
  const start = Math.max(0, seg.start_month);
  const end = Math.min(count - 1, seg.end_month);
  return `<div class="planner-seg planner-${esc(seg.kind)}" title="${esc(segText(seg))}" data-from="${start + 1}" data-to="${end + 2}">
      <span class="planner-seg-icon" aria-hidden="true">${icon(k.icon, "sm")}</span><span>${esc(segText(seg))}</span></div>`;
}

function gridRow(svc, names, count) {
  const body = svc.segments.length
    ? svc.segments.map((seg) => segCell(seg, count)).join("")
    : `<div class="planner-seg planner-none" data-from="1" data-to="${count + 1}">Not planned</div>`;
  return `<div class="planner-row" role="row">
      <div class="planner-name" role="rowheader"><strong>${esc(svc.name)}</strong>${relationHtml(svc, names)}</div>
      <div class="planner-track" role="cell">${body}</div></div>`;
}

function renderGrid(plan) {
  const names = serviceNames(plan);
  const head = monthLabels(plan.months).map((m) => `<span role="columnheader">${esc(m)}</span>`).join("");
  const grid = $("planner-grid");
  grid.setAttribute("role", "table");
  grid.setAttribute("aria-label", `Rotation plan for the next ${plan.months} months`);
  grid.innerHTML = `<div class="planner-row planner-head" role="row"><div class="planner-name" role="columnheader">Service</div>
      <div class="planner-track planner-months">${head}</div></div>
    ${plan.services.map((s) => gridRow(s, names, plan.months)).join("")}`;
  grid.style.setProperty("--planner-months", String(plan.months));
  grid.querySelectorAll(".planner-seg").forEach((el) => {
    el.style.gridColumn = `${el.dataset.from} / ${el.dataset.to}`;
  });
}

// --- Table view ---

function tableCell(svc, month) {
  const seg = svc.segments.find((s) => s.start_month <= month && month <= s.end_month);
  if (!seg) return `<td class="planner-empty">-</td>`;
  return `<td class="planner-${esc(seg.kind)}">${icon(kindOf(seg).icon, "sm")} ${esc(segText(seg))}</td>`;
}

function renderTable(plan) {
  const names = serviceNames(plan);
  const head = monthLabels(plan.months).map((m) => `<th scope="col">${esc(m)}</th>`).join("");
  const rows = plan.services.map((svc) => {
    const cells = svc.segments.length
      ? Array.from({ length: plan.months }, (_, m) => tableCell(svc, m)).join("")
      : `<td class="planner-none" colspan="${plan.months}">Not planned</td>`;
    return `<tr><th scope="row">${esc(svc.name)}${relationHtml(svc, names)}</th>${cells}</tr>`;
  }).join("");
  $("planner-table-wrap").innerHTML = `<table class="tbl planner-table">
    <thead><tr><th scope="col">Service</th>${head}</tr></thead><tbody>${rows}</tbody></table>`;
  $("planner-table-view").open = window.matchMedia(MOBILE).matches;
}

// --- Picks and notes ---

function renderPicks(plan) {
  const names = serviceNames(plan);
  const items = Object.entries(plan.picks).filter(([, list]) => list.length).map(([id, list]) => {
    const titles = list.map((p) => {
      const has = p.interest_score !== null && p.interest_score !== undefined;
      return `${esc(displayTitle(p))}${has ? ` (interest ${esc(p.interest_score)})` : ""}`;
    }).join(", ");
    return `<li><strong>${esc(nameOf(names, id))}</strong>: ${titles}</li>`;
  }).join("");
  $("planner-why").innerHTML = items
    ? `<h4 class="rb-t">Why this service</h4><ul class="planner-list">${items}</ul>` : "";
}

function renderNotes(plan) {
  const count = String(plan.unassigned_titles);
  const link = `<button type="button" id="planner-tidy-link" class="btn sm outline" data-call="openTidy" data-args='[]'>${icon("broom")}Open Tidy</button>`;
  let linked = false;
  const notes = plan.notes.map((n) => {
    const isUnassigned = !linked && plan.unassigned_titles > 0 && n.startsWith(count) && n.includes("no service assigned");
    if (isUnassigned) linked = true;
    return `<li>${esc(n)}${isUnassigned ? ` ${link}` : ""}</li>`;
  });
  if (plan.unassigned_titles > 0 && !linked) notes.push(`<li>${link}</li>`);
  $("planner-notes").innerHTML = notes.join("");
}

const RENEWAL_PREFIX = "Check your renewal day for ";

// One line for all "Check your renewal day for X" notes; every other note is kept as it is.
export function foldRenewalNotes(notes) {
  const names = notes.filter((n) => n.startsWith(RENEWAL_PREFIX)).map((n) => n.slice(RENEWAL_PREFIX.length));
  if (!names.length) return notes;
  const first = notes.findIndex((n) => n.startsWith(RENEWAL_PREFIX));
  const folded = `Renewal day unknown for: ${names.join(", ")} (set it on the Services tab)`;
  return notes.filter((n, i) => !n.startsWith(RENEWAL_PREFIX) || i === first).map((n) => (n.startsWith(RENEWAL_PREFIX) ? folded : n));
}

function renderPlanner(plan) {
  renderSummary(plan);
  renderGrid(plan);
  renderTable(plan);
  renderPicks(plan);
  renderNotes({ ...plan, notes: foldRenewalNotes(plan.notes) });
}

async function ensureOptimizerPlan() {
  if (state.optimizerPlan) return;
  try {
    state.optimizerPlan = await api("/api/optimizer/plan");
  } catch { /* the unused card shows n/a */ }
}

async function loadPlanner() {
  const status = $("planner-status");
  if (!status) return;
  const seq = ++loadSeq;
  status.textContent = "Planning...";
  try {
    const [plan] = await Promise.all([api(`/api/optimizer/rotation?months=${months}`), ensureOptimizerPlan()]);
    if (seq !== loadSeq) return;
    status.textContent = "";
    renderPlanner(plan);
  } catch (err) {
    if (seq === loadSeq) status.textContent = `Could not load the plan: ${err.message}`;
  }
}

function setMonths(el) {
  const n = Number(el.value);
  if (!MONTH_CHOICES.includes(n)) return;
  months = n;
  loadPlanner();
}

onTabActivated("optimizer-tab", loadPlanner);
registerChange("planner-months", setMonths);
register({ loadPlanner });
