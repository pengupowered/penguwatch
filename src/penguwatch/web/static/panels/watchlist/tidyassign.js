// Tidy up: "Assign a service" group (open titles that have no streaming service).
import { api } from "../../js/api.js";
import { refreshAfterChange } from "../../js/data.js";
import { registerAction } from "../../js/dispatch.js";
import { state } from "../../js/state.js";
import { displayTitle, esc, showToast, subtitleHtml } from "../../js/util.js";

function setCount() {
  const left = document.querySelectorAll("#tidy-assign-list .tidy-assign-row").length;
  document.getElementById("tidy-assign-count").textContent = left ? `(${left})` : "";
  if (!left) document.getElementById("tidy-assign-list").innerHTML = '<div class="muted review-empty">Every open title has a service.</div>';
}

export function renderAssign(rows) {
  const list = document.getElementById("tidy-assign-list");
  document.getElementById("tidy-assign-count").textContent = rows.length ? `(${rows.length})` : "";
  if (!rows.length) {
    list.innerHTML = '<div class="muted review-empty">Every open title has a service.</div>';
    return;
  }
  list.innerHTML = rows
    .map((r) => {
      const opts = ['<option value="">Choose a service…</option>']
        .concat(state.services.map((s) =>
          `<option value="${esc(s.id)}" ${s.id === r.suggested_service_id ? "selected" : ""}>${esc(s.name)}</option>`))
        .join("");
      return `<div class="review-row tidy-assign-row" id="tidy-assign-row-${r.id}">
        <span class="review-title"><a class="title-link" href="#" data-call="openShow" data-prevent="1" data-args='[${r.id}]'>${esc(displayTitle(r))}</a>${subtitleHtml(r, "small")}</span>
        <span class="review-desc">${r.hours_watched} h watched${r.suggested_service_id ? " · suggested from its network" : ""}</span>
        <select class="input sm" id="tidy-assign-sel-${r.id}" aria-label="Service for ${esc(displayTitle(r))}">${opts}</select>
        <button class="btn sm outline" data-action="tidy-assign" data-show-id="${r.id}">Apply</button>
      </div>`;
    })
    .join("");
}

registerAction("tidy-assign", async (el) => {
  const id = Number(el.dataset.showId);
  const sel = document.getElementById(`tidy-assign-sel-${id}`);
  if (!sel || !sel.value) return showToast("Choose a service first", true);
  el.disabled = true;
  try {
    await api("/api/tidy/apply", { method: "POST", body: { assignments: [{ show_id: id, service_id: sel.value }] } });
    showToast("Service assigned");
    const row = document.getElementById(`tidy-assign-row-${id}`);
    if (row) row.remove();
    setCount();
    refreshAfterChange();
  } catch (err) {
    el.disabled = false;
    showToast(err.message, true);
  }
});
