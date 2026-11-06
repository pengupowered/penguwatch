// Ratings tab: the rating sources (list, create, edit, clear, delete) and the CSV import. Talks to /api/rating-sources
// and /api/ratings/{source}; the Library and the details dialog follow through the bus ("rating-sources-loaded").
import { api } from "../../js/api.js";
import { loadRatingSources, refreshAfterChange } from "../../js/data.js";
import { register, registerAction, registerChange } from "../../js/dispatch.js";
import { closeModal, openModal } from "../../js/modal.js";
import { on } from "../../js/bus.js";
import { state } from "../../js/state.js";
import { onTabActivated } from "../../js/tabs.js";
import { esc, icon, showToast } from "../../js/util.js";
import { keyProviders } from "../../js/drivers.js";
import { mapColumns, parseCsv, rowToItem, toCsv } from "./csv.js";

const TAB = "ratings-tab";
const BATCH = 500;
const MAX_ROWS = 20000;
const SHOWN = 25;   // rows of each problem list drawn on the page (the download has them all)

const $ = (id) => document.getElementById(id);
const custom = () => state.ratingSources.filter((s) => !s.builtin);
const sourceById = (id) => state.ratingSources.find((s) => s.id === id);
let loaded = null;   // { header, cols, rows: [{cells, item?, error?}] } of the chosen file
let importing = false;

// --- the list ---

function originBadge(s) {
  if (s.builtin) return `<span class="badge quiet st-unknown">${icon("lock")}Built in</span>`;
  if (s.read_only) return `<span class="badge quiet st-unknown">${icon("file")}config.yaml</span>`;
  return `<span class="badge quiet st-good">${icon("ok")}Yours</span>`;
}

function renderSources() {
  const box = $("rating-sources-list");
  if (!box) return;
  const rows = state.ratingSources.map((s) => `<tr>
      <th scope="row">${esc(s.name)}</th>
      <td><span class="rating"><b>${esc(s.label)}</b></span></td>
      <td><code>${esc(s.id)}</code></td>
      <td class="num">${esc(s.scale_min)} to ${esc(s.scale_max)}</td>
      <td>${s.show_on_cards ? "Yes" : "No"}</td>
      <td class="num">${s.rated_titles}</td>
      <td>${originBadge(s)}</td>
      <td class="row-actions">${s.read_only ? "" : `
        <button type="button" class="btn sm outline" data-action="rating-source-edit" data-id="${esc(s.id)}">${icon("edit")}Edit</button>
        <button type="button" class="btn sm outline" data-action="rating-source-clear" data-id="${esc(s.id)}" ${s.rated_titles ? "" : "disabled"}>Clear values</button>
        <button type="button" class="btn sm danger" data-action="rating-source-delete" data-id="${esc(s.id)}">${icon("trash")}Delete</button>`}</td>
    </tr>`).join("");
  box.innerHTML = `<table class="tbl"><thead><tr><th>Name</th><th>Badge</th><th>Id</th><th class="num">Scale</th><th>On cards</th><th class="num">Titles</th><th>From</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
  fillImportSources();
}

function fillImportSources() {
  const select = $("import-source");
  if (!select) return;
  const keep = select.value;
  select.replaceChildren(...custom().map((s) => {
    const o = document.createElement("option");
    o.value = s.id;
    o.textContent = `${s.name} (${s.scale_min} to ${s.scale_max})`;
    return o;
  }));
  if (keep && sourceById(keep)) select.value = keep;
  updateImportButton();
}

// --- create / edit ---

const slug = (text) => String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32);

function showSourceError(message) {
  const el = $("rs-error");
  el.textContent = message;
  el.hidden = !message;
}

function openRatingSource(id) {
  const s = id ? sourceById(id) : null;
  $("rating-source-modal-title").textContent = s ? `Edit ${s.name}` : "New rating source";
  $("rs-editing").value = s ? s.id : "";
  $("rs-name").value = s ? s.name : "";
  $("rs-label").value = s ? s.label : "";
  $("rs-id").value = s ? s.id : "";
  $("rs-id").readOnly = Boolean(s);
  delete $("rs-id").dataset.touched;
  $("rs-id-hint").hidden = Boolean(s);
  $("rs-min").value = s ? s.scale_min : 0;
  $("rs-max").value = s ? s.scale_max : 10;
  $("rs-decimals").value = s ? s.decimals : 1;
  $("rs-cards").checked = s ? s.show_on_cards : true;
  showSourceError("");
  openModal("rating-source-modal");
}

async function saveSource(ev) {
  ev.preventDefault();
  const editing = $("rs-editing").value;
  const body = {
    name: $("rs-name").value.trim(), label: $("rs-label").value.trim(),
    scale_min: Number($("rs-min").value), scale_max: Number($("rs-max").value),
    decimals: parseInt($("rs-decimals").value, 10), show_on_cards: $("rs-cards").checked,
  };
  if (!body.name || !body.label) return showSourceError("Give the source a name and a short badge label.");
  if (!(body.scale_min < body.scale_max)) return showSourceError("The lowest value must be below the highest.");
  try {
    if (editing) await api(`/api/rating-sources/${encodeURIComponent(editing)}`, { method: "PATCH", body });
    else await api("/api/rating-sources", { method: "POST", body: { id: $("rs-id").value.trim(), ...body } });
  } catch (err) {
    return showSourceError(err.message);
  }
  closeModal("rating-source-modal");
  showToast(editing ? "Rating source saved" : "Rating source created");
  await loadRatingSources();
  await refreshAfterChange();
}

async function clearSource(el) {
  const s = sourceById(el.dataset.id);
  if (!s || !confirm(`Remove all ${s.rated_titles} value(s) of ${s.name}? The source stays.`)) return;
  try {
    const r = await api(`/api/ratings/${encodeURIComponent(s.id)}`, { method: "DELETE" });
    showToast(`Removed ${r.removed} value(s)`);
  } catch (err) {
    showToast(err.message, true);
  }
  await loadRatingSources();
  await refreshAfterChange();
}

async function deleteSource(el) {
  const s = sourceById(el.dataset.id);
  if (!s || !confirm(`Delete ${s.name} and its ${s.rated_titles} value(s)? This cannot be undone.`)) return;
  try {
    await api(`/api/rating-sources/${encodeURIComponent(s.id)}`, { method: "DELETE" });
    showToast("Rating source deleted");
  } catch (err) {
    showToast(err.message, true);
  }
  await loadRatingSources();
  await refreshAfterChange();
}

// --- the CSV import ---

function importError(message) {
  const el = $("import-error");
  el.textContent = message;
  el.hidden = !message;
}

// A row can go in when it has an item and its value fits the chosen source's scale (the server would refuse the
// whole batch for one value outside it, so such rows are held back here and listed).
function problemOf(row) {
  if (row.error) return row.error;
  const s = sourceById($("import-source")?.value);
  if (row.item && s && !(row.item.value >= s.scale_min && row.item.value <= s.scale_max)) return `rating outside ${s.scale_min} to ${s.scale_max}`;
  return "";
}

function describeFile() {
  if (!loaded) return;
  const bad = loaded.rows.filter((r) => problemOf(r));
  const reasons = {};
  bad.forEach((r) => { const why = problemOf(r); reasons[why] = (reasons[why] || 0) + 1; });
  $("import-preview").innerHTML = `<b class="num">${loaded.rows.length}</b> rows read: <b class="num">${loaded.rows.length - bad.length}</b> ready to send${bad.length
    ? `, <b class="num">${bad.length}</b> that cannot be sent (${Object.entries(reasons).map(([k, n]) => `${n} with ${esc(k)}`).join(", ")})` : ""}.`;
}

function updateImportButton() {
  describeFile();
  const ready = loaded && loaded.rows.some((r) => r.item && !problemOf(r)) && $("import-source")?.value && !importing;
  const btn = $("btn-run-import");
  if (btn) btn.disabled = !ready;
}

async function fileChosen(input) {
  loaded = null;
  $("import-result").replaceChildren();
  importError("");
  const file = input.files && input.files[0];
  if (!file) { $("import-preview").replaceChildren(); return updateImportButton(); }
  if (file.size > 10 * 1024 * 1024) return importError("That file is larger than 10 MB. Split it and import the parts.");
  const table = parseCsv(await file.text());
  if (table.length < 2) return importError("The file needs a header row and at least one data row.");
  if (table.length - 1 > MAX_ROWS) return importError(`The file has more than ${MAX_ROWS} rows. Split it and import the parts.`);
  const header = table[0];
  const cols = mapColumns(header, keyProviders());
  if (cols.rating < 0) return importError("No rating column found. Name it rating, score or value.");
  const rows = table.slice(1).map((cells) => ({ cells, ...rowToItem(cells, cols) }));
  loaded = { header, cols, rows };
  updateImportButton();
}

const tile = (label, n, cls) => `<div class="panel tile"><div class="stat"><span class="lbl">${esc(label)}</span><span class="val num ${cls || ""}">${n}</span></div></div>`;

function problemTable(title, rows) {
  if (!rows.length) return "";
  const head = loaded.header.map((h) => `<th>${esc(h)}</th>`).join("");
  const body = rows.slice(0, SHOWN).map((r) => `<tr>${loaded.header.map((_, i) => `<td>${esc(r.cells[i] ?? "")}</td>`).join("")}</tr>`).join("");
  return `<h3 class="sub-h">${esc(title)} <span class="muted">(${rows.length}${rows.length > SHOWN ? `, first ${SHOWN} shown` : ""})</span></h3>
    <div class="table-wrap"><table class="tbl"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

let problems = [];   // the rows that did not go in, with why: the download

function renderResult(total, unmatched, ambiguous, invalid, stopped) {
  problems = [...invalid.map((r) => [r, r.error]), ...unmatched.map((r) => [r, "not found"]), ...ambiguous.map((r) => [r, "several titles match"])];
  const box = $("import-result");
  box.innerHTML = `${stopped ? `<div class="result st-crit">${icon("warn")}<span>${esc(stopped)}</span></div>` : ""}
    <div class="tiles">${tile("Updated", total.updated, "ink-good")}${tile("Unchanged", total.unchanged)}${tile("Not found", unmatched.length)}${tile("Ambiguous", ambiguous.length)}${tile("Not sent", invalid.length)}</div>
    ${problemTable("Not found in your library", unmatched)}
    ${problemTable("Several titles match: add a year or an id", ambiguous)}
    ${problemTable("Rows that could not be sent", invalid)}
    ${problems.length ? `<div><button type="button" class="btn outline" data-call="downloadProblemRows">${icon("download")}Download these ${problems.length} rows as CSV</button></div>` : ""}`;
}

async function runRatingImport() {
  const source = $("import-source").value;
  if (!loaded || !source || importing) return;
  importing = true;
  updateImportButton();
  importError("");
  const sendable = loaded.rows.filter((r) => r.item && !problemOf(r));
  const invalid = loaded.rows.filter((r) => problemOf(r)).map((r) => ({ ...r, error: problemOf(r) }));
  const total = { updated: 0, unchanged: 0 };
  const unmatched = [];
  const ambiguous = [];
  let stopped = "";
  const status = $("import-preview");
  try {
    for (let start = 0; start < sendable.length; start += BATCH) {
      const batch = sendable.slice(start, start + BATCH);
      status.textContent = `Sending rows ${start + 1} to ${start + batch.length} of ${sendable.length}…`;
      try {
        const res = await api(`/api/ratings/${encodeURIComponent(source)}`, { method: "PUT", body: { items: batch.map((b) => b.item) } });
        total.updated += res.updated;
        total.unchanged += res.unchanged;
        res.unmatched.forEach((u) => unmatched.push(batch[u.index]));
        res.ambiguous.forEach((a) => ambiguous.push(batch[a.index]));
      } catch (err) {
        stopped = `Stopped at rows ${start + 1} to ${start + batch.length}: ${err.message}. Nothing from that batch was saved; earlier batches were.`;
        break;
      }
    }
  } finally {
    importing = false;
  }
  status.textContent = stopped ? "" : `Done: ${sendable.length} rows sent.`;
  renderResult(total, unmatched, ambiguous, invalid, stopped);
  updateImportButton();
  await loadRatingSources();
  await refreshAfterChange();
}

function downloadProblemRows() {
  if (!problems.length) return;
  const text = toCsv([[...loaded.header, "reason"], ...problems.map(([r, why]) => [...loaded.header.map((_, i) => r.cells[i] ?? ""), why])]);
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = "penguwatch-unmatched-ratings.csv";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

register({ openRatingSource, runRatingImport, downloadProblemRows });
registerAction("rating-source-edit", (el) => openRatingSource(el.dataset.id));
registerAction("rating-source-clear", clearSource);
registerAction("rating-source-delete", deleteSource);
registerChange("import-file-chosen", fileChosen);
on("rating-sources-loaded", renderSources);
// The CSV help names the id columns the drivers add (<name>_id)
on("drivers-loaded", () => {
  const host = $("csv-driver-ids");
  if (host) host.innerHTML = keyProviders().filter((p) => !["imdb", "tmdb", "tvdb"].includes(p)).map((p) => ` <code>${esc(p)}_id</code>,`).join("");
});
onTabActivated(TAB, loadRatingSources);

export default {
  id: "ratings",
  mount() {
    $("rating-source-form").addEventListener("submit", saveSource);
    $("rs-name").addEventListener("input", () => {
      if (!$("rs-editing").value && !$("rs-id").dataset.touched) $("rs-id").value = slug($("rs-name").value);
    });
    $("rs-id").addEventListener("input", () => { $("rs-id").dataset.touched = "1"; });
    $("import-source").addEventListener("change", updateImportButton);
  },
};
