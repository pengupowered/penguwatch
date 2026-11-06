// Tidy up: stale 'watching' shows and finished shows with new seasons (the dialog in tidy.html). The counts also
// feed the Library's button and the note on Up Next ("tidy-counts" on the bus).
import { api } from "../../js/api.js";
import { emit } from "../../js/bus.js";
import { refreshAfterChange, registerLoader } from "../../js/data.js";
import { register } from "../../js/dispatch.js";
import { libraryLabel } from "../../js/drivers.js";
import { onModalClose, openModal } from "../../js/modal.js";
import {
  STATUS_LABELS, checkedValues, displayTitle, esc, icon, showToast, subtitleHtml, syncSelectAll, wireSelectAll,
} from "../../js/util.js";
import { renderAssign } from "./tidyassign.js";

// Allow-list / handler registration (delegated dispatcher in dispatch.js, tab refresh in tabs.js)
register({
  openTidy,
});

// --- Tidy up (stale "watching" + finished shows with new seasons) ---

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(String(iso).slice(0, 10) + "T00:00:00");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

export function setupTidy() {
  document.getElementById("btn-open-tidy").addEventListener("click", openTidy);
  onModalClose("tidy-modal", loadTidyCounts);
  document.getElementById("tidy-months").addEventListener("change", renderTidy);
  wireSelectAll("tidy-returning-all", "tidy-ret-check");
  wireSelectAll("tidy-stale-all", "tidy-stale-check");
  const move = async (cls, targetId) => {
    const boxes = [...document.querySelectorAll(`.${cls}:checked`)];
    const ids = boxes.filter((b) => !b.dataset.sequel && b.value).map((b) => Number(b.value));
    const sequels = boxes.filter((b) => b.dataset.sequel).map((b) => JSON.parse(b.dataset.sequel));
    if (!ids.length && !sequels.length) return showToast("Select at least one show", true);
    const status = document.getElementById(targetId).value;
    try {
      const r = await api("/api/tidy/apply", { method: "POST", body: { show_ids: ids, sequels, status } });
      const parts = [];
      if (r.moved) parts.push(`moved ${r.moved} to ${STATUS_LABELS[status]}`);
      if (r.added) parts.push(`added ${r.added} sequel(s) as ${STATUS_LABELS[status]}`);
      showToast(`${parts.join(", ") || "Nothing to change"}${r.review_mode ? `. Approve in Sync Now to update ${libraryLabel()}` : ""}`);
      await renderTidy();
      refreshAfterChange();
    } catch (err) {
      showToast(err.message, true);
    }
  };
  document.getElementById("btn-tidy-returning-move").addEventListener("click", () => move("tidy-ret-check", "tidy-returning-target"));
  document.getElementById("tidy-stale-target").addEventListener("change", (e) => {
    if (!e.target.value) return;
    document.querySelectorAll(".tidy-stale-check:checked").forEach((c) => {
      const sel = document.getElementById(`tidy-stale-status-${c.value}`);
      if (sel) sel.value = e.target.value;
    });
    e.target.value = "";
  });
  document.getElementById("btn-tidy-stale-move").addEventListener("click", async () => {
    const moves = [...document.querySelectorAll(".tidy-stale-check:checked")]
      .map((c) => ({ show_id: Number(c.value), status: document.getElementById(`tidy-stale-status-${c.value}`).value }))
      .filter((m) => m.status !== "watching");
    if (!moves.length) return showToast("Select at least one show (rows set to Watching are left alone)", true);
    try {
      const r = await api("/api/tidy/apply", { method: "POST", body: { moves } });
      const by = moves.reduce((acc, m) => ((acc[m.status] = (acc[m.status] || 0) + 1), acc), {});
      const summary = Object.entries(by).map(([st, n]) => `${n} → ${STATUS_LABELS[st]}`).join(", ");
      showToast(`Moved ${r.moved}: ${summary}${r.review_mode ? `. Approve in Sync Now to update ${libraryLabel()}` : ""}`);
      await renderTidy();
      refreshAfterChange();
    } catch (err) {
      showToast(err.message, true);
    }
  });
  document.getElementById("btn-tidy-returning-dismiss").addEventListener("click", async () => {
    const keys = [...document.querySelectorAll(".tidy-ret-check:checked")].map((c) => c.dataset.key);
    if (!keys.length) return showToast("Select at least one show", true);
    await api("/api/tidy/dismiss", { method: "POST", body: { kind: "returning", keys } });
    renderTidy();
  });
  document.getElementById("btn-tidy-stale-dismiss").addEventListener("click", async () => {
    const ids = checkedValues("tidy-stale-check").map(Number);
    if (!ids.length) return showToast("Select at least one show", true);
    const months = parseInt(document.getElementById("tidy-months").value, 10);
    await api("/api/tidy/dismiss", { method: "POST", body: { kind: "stale", show_ids: ids, months } });
    showToast(`Kept ${ids.length} show(s) as Watching`);
    renderTidy();
  });
}

function openTidy() {
  openModal("tidy-modal");
  renderTidy();
}

async function loadTidyCounts() {
  try {
    const t = await api("/api/tidy?months=6");
    const btn = document.getElementById("btn-open-tidy");
    const total = t.returning.length + t.stale.length;
    if (btn) btn.querySelector("span").textContent = total ? `Tidy up (${total})` : "Tidy up";
    const available = t.returning.filter((r) => r.state === "available").length;
    emit("tidy-counts", { returning: t.returning.length, stale: t.stale.length, available });
  } catch (err) {
    console.error(err);
  }
}

async function renderTidy() {
  const months = parseInt(document.getElementById("tidy-months").value, 10) || 6;  // a number, never markup
  const retList = document.getElementById("tidy-returning-list");
  const staleList = document.getElementById("tidy-stale-list");
  retList.innerHTML = staleList.innerHTML = '<div class="muted review-empty loading-state">Loading…</div>';
  try {
    const t = await api(`/api/tidy?months=${months}`);
    document.getElementById("tidy-returning-count").textContent = t.returning.length ? `(${t.returning.length})` : "";
    document.getElementById("tidy-stale-count").textContent = t.stale.length ? `(${t.stale.length})` : "";
    renderAssign(t.unassigned || []);
    retList.innerHTML = t.returning.length
      ? t.returning
          .map((r) => {
            if (r.kind === "sequel") {
              const sq = r.sequel;
              const when = r.state === "available"
                ? `<span class="badge quiet st-good">${icon("ok")}${sq.year || ""} · not in library</span>`
                : `<span class="badge quiet st-busy">${icon("clock")}${sq.year ? `Coming ${sq.year}` : "Announced"}</span>`;
              return `<label class="review-row">
                <input type="checkbox" class="tidy-ret-check" value="" data-key="${esc(r.key)}"
                  data-sequel='${esc(JSON.stringify({ ids: sq.ids || {}, title: sq.title, parent_id: r.show.id }))}'>
                <span class="review-title"><a class="title-link" href="#" data-call="openShow" data-prevent="1" data-args='[${r.show.id}]'>${esc(displayTitle(r.show))}</a>${subtitleHtml(r.show, "small")}</span>
                <span class="review-desc">Sequel: <strong>${esc(sq.title)}</strong>${sq.anime_type && sq.anime_type !== "tv" ? ` (${esc(sq.anime_type)})` : ""}. Will be added to your library</span>
                ${when}
              </label>`;
            }
            const what = r.new_season ? `Season ${r.new_season}` : `New episodes from ${esc(r.first_code)}`;
            const when = r.state === "available"
              ? `<span class="badge quiet st-good">${icon("ok")}Out now${r.aired_count ? ` · ${r.aired_count} episode${r.aired_count === 1 ? "" : "s"}` : ""}</span>`
              : `<span class="badge quiet st-busy">${icon("clock")}Premieres ${fmtDate(r.first_air_date)}</span>`;
            return `<label class="review-row">
              <input type="checkbox" class="tidy-ret-check" value="${r.show.id}" data-key="${esc(r.key)}" ${r.state === "available" ? "checked" : ""}>
              <span class="review-title"><a class="title-link" href="#" data-call="openShow" data-prevent="1" data-args='[${r.show.id}]'>${esc(displayTitle(r.show))}</a>${subtitleHtml(r.show, "small")}</span>
              <span class="review-desc">${what}${r.new_count ? ` (${r.new_count} episode${r.new_count === 1 ? "" : "s"})` : ""}</span>
              ${when}
            </label>`;
          })
          .join("")
      : '<div class="muted review-empty">No finished shows have new episodes right now.</div>';
    staleList.innerHTML = t.stale.length
      ? t.stale
          .map((r) => {
            const opts = ["completed", "hold", "dropped", "plantowatch", "watching"]
              .map((st) => `<option value="${st}" ${r.suggested_status === st ? "selected" : ""}>${st === "watching" ? "Keep Watching" : STATUS_LABELS[st]}</option>`)
              .join("");
            return `<label class="review-row">
              <input type="checkbox" class="tidy-stale-check" value="${r.show.id}" ${r.suggested_status === "watching" ? "" : "checked"}>
              <span class="review-title"><a class="title-link" href="#" data-call="openShow" data-prevent="1" data-args='[${r.show.id}]'>${esc(displayTitle(r.show))}</a>${subtitleHtml(r.show, "small")}</span>
              <span class="review-desc">${r.last_watched_at ? `Last watched ${fmtDate(r.last_watched_at)}` : "Never started"}${r.days_idle ? ` (${Math.round(r.days_idle / 30)} mo ago)` : ""} · ${esc(r.reason)}</span>
              <select class="input sm" id="tidy-stale-status-${r.show.id}" aria-label="New status for ${esc(displayTitle(r.show))}">${opts}</select>
            </label>`;
          })
          .join("")
      : `<div class="muted review-empty">Nothing untouched for ${months}+ months. Nice.</div>`;
    syncSelectAll("tidy-returning-all", "tidy-ret-check");
    syncSelectAll("tidy-stale-all", "tidy-stale-check");
  } catch (err) {
    retList.innerHTML = `<div class="empty st st-crit">${icon("warn")}<span>${esc(err.message)}</span></div>`;
    staleList.innerHTML = "";
  }
}

registerLoader(loadTidyCounts);
