// Services & Sync tab: subscription manager (cost, renewal, active state). The library driver's card is library.js, the review
// dialog review.js, the sign-in card session.js, the "Add a service" form addservice.js.
import { api } from "../../js/api.js";
import { on } from "../../js/bus.js";
import { pricesChanged, subscriptionsChanged } from "../../js/data.js";
import { registerAction, registerChange } from "../../js/dispatch.js";
import { state } from "../../js/state.js";
import { esc, icon, showToast } from "../../js/util.js";
import { renderMemberChoices, setupAddService, startEditService } from "./addservice.js";
import { setupReview } from "./review.js";
import { setupSession } from "./session.js";
import { setupLibraryListeners } from "./library.js";

// Allow-list / handler registration (delegated dispatcher in dispatch.js, tab refresh in tabs.js)
registerChange("service-cost", (el) => updateServiceCost(el.dataset.service, el.value));
registerChange("service-renewal", (el) => updateServiceRenewal(el.dataset.service, el.value));
registerChange("service-billing", (el) => saveServiceFields(el.dataset.service, { billing_period: el.value === "yearly" ? "yearly" : "monthly" }, "billing period updated"));
registerChange("service-renewal-date", (el) => saveServiceFields(el.dataset.service, { renewal_date: el.value === "" ? null : el.value }, el.value === "" ? "renewal date cleared" : "renewal date saved"));
registerChange("service-note", (el) => saveServiceFields(el.dataset.service, { notes: el.value.trim() === "" ? null : el.value.trim() }, "note saved"));
registerAction("service-prices", (el) => togglePriceHistory(el.dataset.service));
registerAction("service-edit", (el) => startEditService(el.dataset.service));
registerAction("toggle-service", (el, ev) => {
  const d = el.dataset;
  ev.stopPropagation();
  toggleServiceActive(d.service, d.active === "true");
});

// The list is loaded by data.js (loadServices); this tab draws its manager and the bundle form's choices from it
on("services-loaded", (services) => {
  renderServicesManager(services);
  renderMemberChoices(services);
});

// --- Services ---

async function toggleServiceActive(serviceId, isActive) {
  try {
    const svc = await api(`/api/services/${serviceId}/toggle`, { method: "PUT", body: { is_active: isActive } });
    showToast(`${svc.name} is now ${isActive ? "active" : "inactive"}`);
    await subscriptionsChanged();
  } catch (err) {
    showToast(err.message, true);
  }
}

async function updateServiceRenewal(serviceId, value) {
  const svc = state.services.find((x) => x.id === serviceId);
  if (!svc) return;
  const day = value === "" ? null : parseInt(value, 10);
  if (day !== null && (isNaN(day) || day < 1 || day > 31)) { showToast("Renewal day must be 1 to 31", true); return; }
  try {
    await api("/api/services", { method: "POST", body: { ...svc, renewal_day: day } });
    showToast(day ? `${svc.name} renews on day ${day}` : `${svc.name}: renewal day cleared`);
    await pricesChanged();
  } catch (err) {
    showToast(err.message, true);
  }
}

// A yearly service shows its price per year; monthly_cost always stays the monthly equivalent.
const toMonthly = (svc, value) => (svc.billing_period === "yearly" ? Math.round((value / 12) * 100) / 100 : value);
const perPeriodPrice = (svc) => (svc.billing_period === "yearly" ? svc.monthly_cost * 12 : svc.monthly_cost);

async function updateServiceCost(serviceId, value) {
  const svc = state.services.find((s) => s.id === serviceId);
  const cost = parseFloat(value);
  if (!svc || isNaN(cost)) return;
  const monthly = toMonthly(svc, cost);
  try {
    await api("/api/services", { method: "POST", body: { ...svc, monthly_cost: monthly } });
    showToast(svc.billing_period === "yearly" ? `${svc.name}: $${cost.toFixed(2)}/year` : `${svc.name}: $${cost.toFixed(2)}/month`);
    await pricesChanged();
  } catch (err) {
    showToast(err.message, true);
  }
}

// "Price history" disclosure: the list is fetched when it is first opened and on every reopen.
async function togglePriceHistory(serviceId) {
  const panel = document.getElementById(`prices-${serviceId}`);
  if (!panel) return;
  if (!panel.hidden) { panel.hidden = true; return; }
  const svc = state.services.find((s) => s.id === serviceId);
  const yearly = svc && svc.billing_period === "yearly";
  try {
    const rows = await api(`/api/services/${encodeURIComponent(serviceId)}/prices`);
    panel.innerHTML = rows.length
      ? `<ul>${rows.map((r) => `<li>${esc(String(r.effective_from).slice(0, 10))}: $${(yearly ? r.monthly_cost * 12 : r.monthly_cost).toFixed(2)} / ${yearly ? "year" : "month"}</li>`).join("")}</ul>`
      : '<span class="muted">No price recorded yet</span>';
    panel.hidden = false;
  } catch (err) {
    showToast(err.message, true);
  }
}

// Saves changed fields of one service (the full object is posted, as the cost handler does).
async function saveServiceFields(serviceId, fields, message) {
  const svc = state.services.find((s) => s.id === serviceId);
  if (!svc) return;
  try {
    await api("/api/services", { method: "POST", body: { ...svc, ...fields } });
    showToast(`${svc.name}: ${message}`);
    await pricesChanged();
  } catch (err) {
    showToast(err.message, true);
  }
}

// service id -> the active bundle that covers it (cheapest wins, ties by bundle id; bundles never cover bundles)
export function coverageMap(services) {
  const isBundle = (s) => Array.isArray(s.includes) && s.includes.length > 0;
  const byId = new Map(services.map((s) => [s.id, s]));
  const covered = new Map();
  services
    .filter((b) => isBundle(b) && b.is_active)
    .sort((a, b) => a.monthly_cost - b.monthly_cost || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
    .forEach((b) => {
      b.includes.forEach((mid) => {
        const m = byId.get(mid);
        if (m && !isBundle(m) && mid !== b.id && !covered.has(mid)) covered.set(mid, b);
      });
    });
  return covered;
}

// Price, billing period, renewal (day of month or exact date for a yearly charge) and note of one service.
function priceRow(s, by) {
  const yearly = s.billing_period === "yearly";
  const id = esc(s.id);
  const title = by ? `Included in ${esc(by.name)}: no separate charge while the bundle is active` : yearly ? "Yearly price" : "Monthly price";
  const renewal = yearly
    ? `<input class="input sm cost-input renew-input renew-date" type="date" value="${esc(s.renewal_date || "")}"
         data-change="service-renewal-date" data-service="${id}" title="Date of the next (or any past) yearly charge" aria-label="${esc(s.name)} renewal date">`
    : `<input class="input sm cost-input renew-input" type="number" min="1" max="31" step="1"
         value="${s.renewal_day || ""}" placeholder="–" data-change="service-renewal" data-service="${id}"
         title="Day of the month you're billed (enables cancel-before-renewal advice and alerts)" aria-label="${esc(s.name)} renewal day">`;
  return `<div class="svc-item-cost">$<input class="input sm cost-input" type="number" step="0.01" min="0" value="${perPeriodPrice(s).toFixed(2)}"
       data-change="service-cost" data-service="${id}" title="${title}"
       aria-label="${esc(s.name)} ${yearly ? "yearly" : "monthly"} price" ${by ? "disabled" : ""}> / ${yearly ? "year" : "month"}
     <select class="input sm cost-input billing-select" data-change="service-billing" data-service="${id}" aria-label="${esc(s.name)} billing period">
       <option value="monthly" ${yearly ? "" : "selected"}>Monthly</option><option value="yearly" ${yearly ? "selected" : ""}>Yearly</option>
     </select>
     <span class="svc-renew">· renews ${yearly ? "on" : "on day"} ${renewal}</span></div>
   <div class="svc-item-note"><input class="input sm cost-input note-input" type="text" maxlength="120" value="${esc(s.notes || "")}"
       placeholder="Note, e.g. included with your phone plan" data-change="service-note" data-service="${id}" aria-label="${esc(s.name)} note"></div>`;
}

function renderServicesManager(services) {
  const container = document.getElementById("services-manager-list");
  if (!container) return;
  const covered = coverageMap(services);
  const names = new Map(services.map((s) => [s.id, s.name]));
  container.innerHTML = services
    .map((s) => {
      const by = covered.get(s.id);
      const members = (s.includes || []).map((id) => names.get(id) || id);
      return `
      <div class="svc-item ${s.is_active ? "is-active" : ""}">
        <div class="svc-item-left">
          <div>
            <div class="svc-item-title">${esc(s.name)}${by ? ` <span class="badge quiet st-good svc-covered-chip" title="Its cost is part of ${esc(by.name)}">${icon("ok")}Covered by ${esc(by.name)}</span>` : ""}</div>
            ${members.length ? `<div class="svc-includes">Includes: ${members.map(esc).join(", ")}</div>` : ""}
            ${!members.length && !by && s.monthly_cost === 0 ? `<div class="svc-includes">Included, no extra cost${s.notes ? `: ${esc(s.notes)}` : ""}</div>` : ""}
            ${priceRow(s, by)}
            <button class="btn-link svc-prices-toggle" type="button" data-action="service-prices" data-service="${esc(s.id)}">Price history</button>
            <button class="btn-link svc-edit" type="button" data-action="service-edit" data-service="${esc(s.id)}" aria-label="Edit ${esc(s.name)}">Edit</button>
            <div class="svc-prices" id="prices-${esc(s.id)}" hidden></div>
          </div>
        </div>
        <div>
          <button class="btn sm svc-toggle ${s.is_active ? "st-good" : "st-unknown"}" data-action="toggle-service" data-service="${esc(s.id)}" data-active="${!s.is_active}" aria-pressed="${s.is_active}" title="${s.is_active ? "Subscribed this month: click to mark it cancelled" : "Not subscribed: click to mark it active"}">
            ${icon(s.is_active ? "ok" : "pause")}${s.is_active ? "Active" : "Inactive"}
          </button>
        </div>
      </div>`;
    })
    .join("");
}

export default {
  id: "services",
  mount() {
    setupLibraryListeners();
    setupReview();
    setupAddService();
    setupSession();
  },
};
