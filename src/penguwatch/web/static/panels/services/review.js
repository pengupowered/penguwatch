// Review of pending outgoing / incoming changes between PenguWatch and the library driver's account (the dialog in review.html).
import { api } from "../../js/api.js";
import { refreshAfterChange } from "../../js/data.js";
import { libraryLabel } from "../../js/drivers.js";
import { register } from "../../js/dispatch.js";
import { closeModal, isOpen, openModal } from "../../js/modal.js";
import { checkLibraryStatus } from "../../js/library-status.js";
import { STATUS_LABELS, checkedValues, displayTitle, esc, icon, showToast, subtitleHtml } from "../../js/util.js";

const FIELD_LABELS = { status: "Status", rating: "Rating", episodes: "Episodes watched", watched: "Episodes watched", library: "Library" };

function fmtValue(field, v) {
  if (v === null || v === undefined || v === "") return "—";
  if (field === "status") return STATUS_LABELS[v] || v;
  if (field === "rating") return `${v}/10`;
  return esc(v);
}

function describeChange(c) {
  let txt = `<strong>${FIELD_LABELS[c.field] || esc(c.field)}:</strong> ${fmtValue(c.field, c.local)} → ${fmtValue(c.field, c.remote)}`;
  const extra = [];
  if (c.added) extra.push(`+ ${esc(c.added)}`);
  if (c.removed) extra.push(`− ${esc(c.removed)}`);
  if (extra.length) txt += ` <span class="muted">(${extra.join("; ")})</span>`;
  return txt;
}

// "Decline" takes the account's version, which needs the account: it is off while a ticked row belongs to a library
// that is not the connected one now (the row says why). Approving such rows stays possible: they wait.
function updateDiscardState() {
  const btn = document.getElementById("btn-outgoing-discard-sel");
  const blocked = [...document.querySelectorAll(".out-check:checked")].find((c) => c.dataset.blocked);
  btn.disabled = Boolean(blocked);
  btn.title = blocked ? blocked.dataset.blocked : btn.dataset.tipDefault || btn.title;
  if (!btn.dataset.tipDefault) btn.dataset.tipDefault = btn.title;
  document.getElementById("outgoing-blocked-note").textContent = blocked ? blocked.dataset.blocked : "";
  document.getElementById("btn-outgoing-drop-sel").hidden = !document.querySelector(".out-check:checked[data-blocked]");
}

export async function renderReview() {
  try {
    const r = await api("/api/sync/review");
    const out = r.outgoing || [];
    const inc = r.incoming || [];
    const waiting = out.filter((o) => !o.approved).length;
    document.getElementById("outgoing-count").textContent = out.length ? `(${out.length})` : "";
    document.getElementById("btn-open-review").querySelector("span").textContent = waiting + inc.length
      ? `Review pending changes (${waiting + inc.length})`
      : "Review pending changes";
    document.getElementById("incoming-count").textContent = inc.length ? `(${inc.length})` : "";

    document.getElementById("outgoing-list").innerHTML = out.length
      ? out
          .map((o) => {
            const stateTxt = !o.approved
              ? `<span class="badge quiet st-busy">${icon("clock")}Waiting for approval</span>`
              : o.attempts
              ? `<span class="badge quiet st-crit" title="${esc(o.last_error || "")}">${icon("warn")}Retrying (${o.attempts})</span>`
              : o.discard_blocked  // its driver is not the connected library now: it waits
              ? `<span class="badge quiet st-busy" title="${esc(o.discard_blocked)}">${icon("clock")}Approved, waiting</span>`
              : `<span class="badge quiet st-good">${icon("sync")}Approved, sending…</span>`;
            return `<label class="review-row">
              ${o.approved ? "<span></span>" : `<input type="checkbox" class="out-check" value="${o.id}" checked${o.discard_blocked ? ` data-blocked="${esc(o.discard_blocked)}"` : ""}>`}
              <span class="review-title">${o.title ? `${esc(displayTitle(o))}${subtitleHtml(o)}` : esc("#" + (o.remote_id ?? o.id))}</span>
              <span class="review-desc">${esc(o.summary || o.op)}</span>
              ${stateTxt}
            </label>`;
          })
          .join("")
      : `<div class="empty">${icon("ok")}<span>Nothing waiting. Your changes are all on ${esc(libraryLabel())}.</span></div>`;

    document.getElementById("incoming-list").innerHTML = inc.length
      ? inc
          .map((e) => {
            const kind = { new: `New on ${libraryLabel()}`, update: `Changed on ${libraryLabel()}`, removed: `Removed on ${libraryLabel()}` }[e.kind] || e.kind;
            return `<label class="review-row">
              <input type="checkbox" class="in-check" value="${esc(e.key)}" checked>
              <span class="review-title">${esc(displayTitle(e))} <span class="chip review-kind kind-${esc(e.kind)}">${kind}</span>${subtitleHtml(e)}</span>
              <span class="review-desc">${(e.changes || []).map(describeChange).join("<br>")}</span>
            </label>`;
          })
          .join("")
      : `<div class="empty">${icon("ok")}<span>Nothing new from ${esc(libraryLabel())}${r.incoming_checked_at ? "" : " since the last check"}.</span></div>`;
    updateDiscardState();
    return { out: waiting, inc: inc.length };
  } catch (err) {
    console.error(err);
    return null;
  }
}

export function openReview() {
  openModal("review-modal");
  renderReview();
}

function closeReview() {
  closeModal("review-modal");
}

async function afterReviewAction() {
  const counts = await renderReview();
  await refreshAfterChange();
  if (counts && counts.out === 0 && counts.inc === 0 && isOpen("review-modal")) {
    closeReview();
    showToast(`PenguWatch and ${libraryLabel()} are in sync`);
  }
}

export function setupReview() {
  document.getElementById("review-mode-toggle").addEventListener("change", async (e) => {
    await api("/api/sync/review/mode", { method: "POST", body: { enabled: e.target.checked } });
    showToast(e.target.checked ? "Changes will wait for your approval" : "Changes will sync automatically");
    checkLibraryStatus();
  });
  const outAction = async (path, body, msg) => {
    try {
      const r = await api(path, { method: "POST", body });
      showToast(msg(r));
      await afterReviewAction();
    } catch (err) {
      showToast(err.message, true);
    }
  };
  document.getElementById("btn-open-review").addEventListener("click", openReview);
  document.getElementById("outgoing-list").addEventListener("change", updateDiscardState);
  document.getElementById("btn-approve-everything").addEventListener("click", async () => {
    try {
      const inc = await api("/api/sync/incoming/resolve", { method: "POST", body: { keys: null, accept: true } });
      const out = await api("/api/sync/outgoing/approve", { method: "POST", body: {} });
      showToast(out.last_error ? out.last_error : `Applied ${inc.resolved} from ${libraryLabel()}, sent ${out.sent} to ${libraryLabel()}`, !!out.last_error);
      await afterReviewAction();
    } catch (err) {
      showToast(err.message, true);
    }
  });
  document.getElementById("btn-outgoing-send-sel").addEventListener("click", () => {
    const ids = checkedValues("out-check").map(Number);
    if (!ids.length) return showToast("Select at least one change", true);
    outAction("/api/sync/outgoing/approve", { ids }, (r) => (r.last_error ? r.last_error : `Sent ${r.sent} change(s) to ${libraryLabel()}`));
  });
  document.getElementById("btn-outgoing-discard-sel").addEventListener("click", () => {
    const ids = checkedValues("out-check").map(Number);
    if (!ids.length) return showToast("Select at least one change", true);
    if (!confirm(`Discard ${ids.length} change(s)? Those titles will be restored to what ${libraryLabel()} has.`)) return;
    outAction("/api/sync/outgoing/discard", { ids }, (r) => `Discarded ${r.discarded}, restored ${r.restored} from ${libraryLabel()}`);
  });
  // The way out for rows that cannot be declined: removes them, restores nothing
  document.getElementById("btn-outgoing-drop-sel").addEventListener("click", () => {
    const ids = [...document.querySelectorAll(".out-check:checked[data-blocked]")].map((c) => Number(c.value));
    if (!ids.length) return;
    if (!confirm(`Drop ${ids.length} change(s) without restoring? They are deleted and the account never receives them. Nothing is fetched now: this device keeps its current state until the account's version of those titles comes in with a later sync.`)) return;
    outAction("/api/sync/outgoing/discard", { ids, restore: false }, (r) => `Dropped ${r.discarded} change(s)`);
  });
  const resolve = (keys, accept) =>
    outAction("/api/sync/incoming/resolve", { keys, accept }, (r) =>
      accept ? `Applied ${r.resolved} change(s) from ${libraryLabel()}` : `Kept ${r.resolved} as-is; sending the version here to ${libraryLabel()}`
    );
  document.getElementById("btn-incoming-accept-sel").addEventListener("click", () => {
    const keys = checkedValues("in-check");
    if (!keys.length) return showToast("Select at least one change", true);
    resolve(keys, true);
  });
  document.getElementById("btn-incoming-keep-sel").addEventListener("click", () => {
    const keys = checkedValues("in-check");
    if (!keys.length) return showToast("Select at least one change", true);
    resolve(keys, false);
  });
}

// The header's "to review" chip opens the dialog (markup data-call)
register({ openReview });
