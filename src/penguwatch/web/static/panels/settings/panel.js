// Settings: every setting of PenguWatch on one tab, and the webhooks list. The tab shows the server's state
// (GET /api/settings, laid out by GET /api/settings/schema) and keeps a draft of changes; nothing is sent until Save
// (PUT /api/settings with the revision the tab loaded; a 409 means a part you touched changed elsewhere).
//
// store.js holds the state, the draft and the calls, model.js the arithmetic, fields.js one row, secrets.js the
// write-only slots, lists/webhooks.js the webhooks editor. The other modals of this folder (versions, export, import,
// confirm) are sibling .html files.
import { registerAction, registerChange } from "../../js/dispatch.js";
import { closeModal, onModalClose, openModal } from "../../js/modal.js";
import { onTabActivated } from "../../js/tabs.js";
import { state } from "../../js/state.js";
import { esc, icon, showToast } from "../../js/util.js";
import { choose, driverOp, edit, personOp, refreshRows, rowHtml } from "./fields.js";
import { loadEvents, mountWebhooks, syncProblems, webhookProblems, wireWebhooks } from "./lists/webhooks.js";
import { refreshSlots, resetEditing, secretInput, slotHtml } from "./secrets.js";
import {
  S, applyDocument, changeCount, changed, clearDraft, dropConflicts, fetchExport, fetchState, fetchVersion, fetchVersions, hasProblems,
  importYaml, putDraft, setValue, validateDraft, watchDraft,
} from "./store.js";

const TAB = "settings-tab";
const $ = (id) => document.getElementById(id);
const pane = () => $(TAB);

let saving = false;
let rendering = false;  // the page is being rebuilt: the change a focused box fires as it goes is not an edit
let loaded = false;
let draftSource = "";   // "" | "restore" (a loaded version) | "yaml_import" (an imported file): sent as ?source= on Save
let versions = null;    // GET /api/settings/versions, once read
let confirmed = null;   // what the confirm dialog's yes does
let cancelled = null;   // what its no does
let validateTimer = 0;
let validateSeq = 0;
let notes = [];         // [{level, html, validation?}] under the page header

// ---------- notes ----------
function setNotes(list, quiet = false) {
  notes = list;
  const box = $("settings-notes");
  box.innerHTML = notes.map((n) => `<div class="result st-${n.level}" data-note>${icon(n.level === "crit" ? "warn" : n.level === "warn" ? "warn" : "info")}<span>${n.html}</span></div>`).join("");
  if (!quiet && notes.length) box.scrollIntoView({ block: "nearest" });
}
const addNote = (level, html) => setNotes([...notes, { level, html }]);

// ---------- the page ----------
const sentence = (names) => names.map((n) => (S.spec[n] ? `${S.spec[n].group ? `${S.spec[n].group}: ` : ""}${S.spec[n].label}` : n)).join(", ");

function groupsOf(sectionId) {
  const specs = S.schema.settings.filter((s) => s.section === sectionId).sort((a, b) => a.order - b.order);
  const groups = [];
  specs.forEach((s) => {
    let g = groups.find((x) => x.name === (s.group || ""));
    if (!g) { g = { name: s.group || "", specs: [] }; groups.push(g); }
    g.specs.push(s);
  });
  return groups;
}

const RESTART_HELP = "These apply after a restart. In a container run <code>docker compose restart</code>; as a background service run <code>penguwatch service install</code> again (it restarts the service); with <code>penguwatch serve</code>, stop it and start it again. There is no restart button: a plain <code>serve</code> has nothing to bring it back.";

function secretsHtml(sectionId) {
  return S.schema.secrets.filter((s) => s.section === sectionId && !s.pattern).map((s) => slotHtml(s.slot, s)).join("");
}

function groupHtml(section, group) {
  const rows = group.specs.map(rowHtml).join("");
  const extra = section.id === "notifications" && group.name === "Daily digest" ? secretsHtml("notifications") : "";
  return `<div class="set-group">${group.name ? `<h3 class="set-group-t">${esc(group.name)}</h3>` : ""}${rows}${extra}</div>`;
}

function sectionHtml(section) {
  let groups = groupsOf(section.id);
  if (section.id === "notifications" && !groups.some((g) => g.name === "Daily digest")) groups = [{ name: "Daily digest", specs: [] }, ...groups];
  const tail = section.id === "notifications" ? '<div class="set-list-box" data-box="webhooks"></div>' : "";
  const lead = section.id === "sources" ? `<p class="fine">${RESTART_HELP}</p>` : "";
  const body = `${lead}${groups.map((g) => groupHtml(section, g)).join("")}${tail}`;
  if (section.id === "advanced") {
    return `<details class="panel set-sec set-adv" id="set-sec-advanced" data-section="advanced"><summary class="panel-h">${icon("chevron", "set-chev")}<h2 class="panel-t">${esc(section.title)}</h2><span class="panel-m">Change these only if you know why.</span></summary><div class="panel-b">${body}</div></details>`;
  }
  return `<section class="panel set-sec" id="set-sec-${esc(section.id)}" data-section="${esc(section.id)}" aria-labelledby="set-sec-${esc(section.id)}-t"><div class="panel-h"><h2 class="panel-t" id="set-sec-${esc(section.id)}-t">${esc(section.title)}</h2></div><div class="panel-b">${body}</div></section>`;
}

function elsewhereHtml() {
  return `<section class="panel set-sec" id="set-sec-elsewhere" data-section="elsewhere" aria-labelledby="set-sec-elsewhere-t"><div class="panel-h"><h2 class="panel-t" id="set-sec-elsewhere-t">Kept elsewhere</h2></div><div class="panel-b">
    <ul class="set-links">
      <li><a href="#/services">Services &amp; Sync</a> <span class="fine">services, prices, aliases and review mode</span></li>
      <li><a href="#/ratings">Ratings</a> <span class="fine">rating sources</span></li>
      <li><a href="#/services">Services &amp; Sync</a> <span class="fine">the sign-in of your library account</span></li>
    </ul></div></section>`;
}

const when = (ts) => new Date(ts * 1000).toLocaleString([], { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });

function aboutHtml() {
  const a = S.server.about;
  if (!a) return "";
  const rows = [["Version", a.version], ["Listens on", a.listen], ["Data folder", a.data_dir], ["Config file", a.config_path || "the example inside the package"],
    ["Database", a.db_path], ["Secrets file", a.secrets_path], ["Access token", a.token_set ? "set" : "not set"]];
  const cfg = a.config_yaml && a.config_yaml.ignored_since
    ? `<p class="fine">config.yaml was read once on ${esc(when(a.config_yaml.ignored_since))}; settings are managed on this tab now and the file is ignored for them.${a.config_yaml.differing_keys ? ` ${a.config_yaml.differing_keys} value${a.config_yaml.differing_keys === 1 ? " in the file differs" : "s in the file differ"} from this tab.` : ""}${a.config_yaml.differing_services ? ` ${a.config_yaml.differing_services} service${a.config_yaml.differing_services === 1 ? " differs" : "s differ"} from the Services tab.` : ""}</p>` : "";
  const report = (a.import_report || []).length
    ? `<details><summary class="fine">The first import left ${a.import_report.length} thing${a.import_report.length === 1 ? "" : "s"} out</summary><ul class="fine">${a.import_report.map((r) => `<li>${esc(r.key)}: ${esc(r.reason)}</li>`).join("")}</ul></details>` : "";
  return `<section class="panel set-sec" id="set-sec-about" data-section="about" aria-labelledby="set-sec-about-t"><div class="panel-h"><h2 class="panel-t" id="set-sec-about-t">About this install</h2></div><div class="panel-b">
    <dl class="set-about">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>${cfg}${report}</div></section>`;
}

function renderAll() {
  const sections = [...S.schema.sections].sort((a, b) => a.order - b.order);
  const top = window.scrollY;
  resetEditing();
  rendering = true;
  if (document.activeElement instanceof HTMLElement && pane().contains(document.activeElement)) document.activeElement.blur();
  $("settings-sections").innerHTML = sections.map(sectionHtml).join("") + elsewhereHtml() + aboutHtml();
  rendering = false;
  const nav = [...sections.map((s) => [s.id, s.title]), ["elsewhere", "Kept elsewhere"], ["about", "About this install"]];
  $("settings-rail").innerHTML = nav.map(([id, title]) => `<button type="button" data-action="settings-goto" data-arg="${esc(id)}">${esc(title)}</button>`).join("");
  $("settings-jump").innerHTML = nav.map(([id, title]) => `<option value="${esc(id)}">${esc(title)}</option>`).join("");
  const box = pane().querySelector('[data-box="webhooks"]');
  if (box) mountWebhooks(box);
  refreshRows();
  updateChrome();
  stickJump();
  window.scrollTo({ top });
}

// The jump list sticks below the header (the header's height is not a token).
function stickJump() {
  const bar = document.querySelector(".topbar");
  $("settings-jump").style.top = `${bar ? bar.offsetHeight : 0}px`;
}

const savedAgo = (ts) => {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return when(ts);
};

function updateChrome() {
  if (!S.server) return;
  const n = changeCount();
  const save = $("settings-save");
  save.querySelector("span").textContent = n ? `Save ${n} change${n === 1 ? "" : "s"}` : "Save";
  save.disabled = n === 0 || hasProblems() || saving;
  save.title = hasProblems() ? "Fix the marked fields first" : "";
  $("settings-cancel").disabled = n === 0 || saving;
  $("settings-count").textContent = n ? `${n} change${n === 1 ? "" : "s"}` : "";
  const bar = $("settings-bar");
  const wasDirty = bar.classList.contains("dirty");
  bar.classList.toggle("dirty", n > 0);
  if (n > 0 && !wasDirty) clearOfBar();
  const ago = versions && versions[0] && versions[0].revision === S.server.revision ? ` · saved ${savedAgo(versions[0].applied_at)}` : "";
  $("settings-meta").textContent = n && hasProblems() ? `${n} unsaved change${n === 1 ? "" : "s"} · Save is off until the marked fields are fixed`
    : n ? `${n} unsaved change${n === 1 ? "" : "s"}` : `revision ${S.server.revision}${ago} · nothing changes until Save`;
  const banner = $("settings-banner");
  const bannerHtml = S.server.token_set ? ""
    : `<div class="result st-warn" data-banner="no-token">${icon("warn")}<span><b>No access token set:</b> anyone who can reach this port can change these settings, including where notifications are sent. Set <code>PENGUWATCH_ACCESS_TOKEN</code> if it is reachable beyond this PC.</span></div>`;
  if (banner._html !== bannerHtml) { banner.innerHTML = bannerHtml; banner._html = bannerHtml; }
}

// On a phone the bar (shown while there is a draft) floats above the tab bar: scroll the focused field out from under it.
function clearOfBar() {
  requestAnimationFrame(() => {
    const bar = $("settings-bar"), f = document.activeElement;
    if (!bar.classList.contains("dirty") || getComputedStyle(bar).position !== "sticky" || !f || !f.matches("input, select, textarea") || !$("settings-tab").contains(f)) return;
    const over = f.getBoundingClientRect().bottom - bar.getBoundingClientRect().top;
    if (over > -8 && bar.getBoundingClientRect().height < innerHeight) window.scrollBy(0, over + 16);
  });
}
document.addEventListener("focusin", (ev) => { if ($("settings-tab").contains(ev.target)) clearOfBar(); });

// ---------- the draft changed: refresh what depends on it, and ask the server to validate ----------
watchDraft(() => {
  if (!S.server) return;
  syncProblems();
  refreshRows();
  refreshSlots();
  updateChrome();
  clearTimeout(validateTimer);
  validateTimer = setTimeout(runValidate, 400);
});

function showServerErrors(errors, warnings) {
  S.errors = {};
  errors.forEach((e) => { if (!(e.where in S.errors)) S.errors[e.where] = e.message; });
  S.warnings = warnings || [];
  const perList = {};
  const other = [];
  Object.keys(S.errors).forEach((where) => {
    const m = /^lists\.webhooks\[(\d+)\]\.?(.*)$/.exec(where);
    if (m) perList[`${m[1]}.${m[2]}`] = S.errors[where];
    else if (!/^(values|secrets)\./.test(where)) other.push(`${esc(where)}: ${esc(S.errors[where])}`);
  });
  webhookProblems(perList);
  refreshRows();
  refreshSlots();
  const general = [...other, ...S.warnings.filter((w) => !/^values\./.test(w.where)).map((w) => esc(w.message))];
  setNotes(notes.filter((n) => !n.validation).concat(general.map((html) => ({ level: "warn", html, validation: true }))), true);
  updateChrome();
}

async function runValidate() {
  const seq = ++validateSeq;
  if (changeCount() === 0) { showServerErrors([], []); return; }
  try {
    const body = await validateDraft();
    if (!body || seq !== validateSeq) return;
    showServerErrors(body.errors || [], body.warnings || []);
    S.summary = body.summary || "";
  } catch {
    // the server decides at Save
  }
}

// ---------- loading ----------
async function loadTab(force = false) {
  if (saving || (changeCount() && !force)) return;
  const box = $("settings-sections");
  try {
    await fetchState();
  } catch (e) {
    if (!loaded) box.innerHTML = "";
    setNotes([{ level: "crit", html: `<b>Settings could not be loaded.</b> ${esc(e.message)}` }]);
    return;
  }
  loaded = true;
  setNotes([]);
  clearDraft();
  renderAll();
  refreshVersions();
  loadEvents();
}

// Revision 0 is the read-only stub (no store, so no versions to ask for).
function refreshVersions() {
  if (!S.server || !(S.server.revision > 0)) { versions = null; return; }
  fetchVersions().then((list) => { versions = list; updateChrome(); }).catch(() => {});
}

function goto(id) {
  const target = $(`set-sec-${id}`);
  if (!target) return;
  if (target.tagName === "DETAILS") target.open = true;
  target.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  $("settings-jump").value = id;
}

// ---------- the confirm dialog ----------
function ask({ title, text, yes, no = "Keep editing" }, onYes, onNo) {
  $("settings-confirm-title").textContent = title;
  $("settings-confirm-text").textContent = text;
  $("settings-confirm-yes").textContent = yes;
  $("settings-confirm-no").textContent = no;
  confirmed = onYes;
  cancelled = onNo || null;
  openModal("settings-confirm-modal");
}

function answer(yes) {
  const fn = yes ? confirmed : null;
  confirmed = null;
  if (yes) cancelled = null;
  closeModal("settings-confirm-modal");
  if (fn) fn();
}

// Escape, a click outside or "no" closes the dialog without an answer: that counts as "no"
onModalClose("settings-confirm-modal", () => {
  const fn = cancelled;
  confirmed = null;
  cancelled = null;
  if (fn) fn();
});

// Replacing the draft (a version, an imported file) asks when there is one.
function replaceDraft(text, fn) {
  if (!changeCount()) { fn(); return; }
  ask({ title: "Replace the draft?", text, yes: "Replace" }, fn);
}

function discard() {
  const n = changeCount();
  if (!n) return;
  ask({ title: "Discard changes?", text: `Discard ${n} unsaved change${n === 1 ? "" : "s"}?`, yes: "Discard" }, () => { clearDraft(); draftSource = ""; setNotes([]); renderAll(); $("settings-title").focus(); });
}

// ---------- Save ----------
async function save() {
  if (saving || !changeCount() || hasProblems()) return;
  saving = true;
  updateChrome();
  try {
    const { status, ok, data } = await putDraft(draftSource);
    if (ok) await saved(data);
    else if (status === 409) await conflicted(data);
    else if (status === 400 || status === 422) {
      showServerErrors(data.errors || [], []);
      addNote("crit", `<b>Not saved.</b> ${esc(data.detail || "The server refused the change.")}`);
    } else addNote("crit", `<b>Not saved.</b> ${esc(data.detail || `The server answered ${status}.`)}`);
  } catch {
    addNote("crit", "<b>Not saved.</b> The server could not be reached.");
  } finally {
    saving = false;
    updateChrome();
  }
}

async function saved(result) {
  showToast(result.summary || "Settings saved");
  const after = [];
  if ((result.live_failed || []).length) after.push({ level: "warn", html: `<b>Saved, but not applied yet:</b> ${esc(sentence(result.live_failed))}. It applies at the next start.` });
  if ((result.restart_needed || []).length) after.push({ level: "warn", html: `<b>Applies after a restart:</b> ${esc(sentence(result.restart_needed))}.` });
  clearDraft();
  draftSource = "";
  try { await fetchState(); } catch { after.push({ level: "warn", html: "Saved. The tab could not re-read the settings; reload it." }); }
  setNotes(after);
  if (S.server) { renderAll(); refreshVersions(); loadEvents(); }
  if (result.reload) setTimeout(() => location.reload(), 1200);   // the household names are in the page's <meta>
}

async function conflicted(data) {
  const conflicts = data.conflicts || [];
  try { await fetchState(); } catch { addNote("crit", "<b>Changed elsewhere,</b> and the tab could not re-read the settings."); return; }
  const dropped = dropConflicts(conflicts);
  setNotes([{ level: "warn", html: `<b>Changed elsewhere since you opened this tab (now at revision ${esc(data.revision ?? S.server.revision)}); kept theirs:</b> ${esc(sentence(dropped.length ? dropped : conflicts))}. Your other changes are still here: press Save again.` }]);
  renderAll();
  changed();
}

// ---------- versions, export, import ----------
function versionsHtml(list) {
  const SOURCES = { import: "first import", save: "saved", restore: "restored", yaml_import: "imported file" };
  const rows = list.map((v) => `<li><span class="set-ver-t"><b>Revision ${v.revision}</b>${v.revision === S.server.revision ? ' <span class="chip">current</span>' : ""}
      <span class="muted">${esc(when(v.applied_at))} · ${esc(SOURCES[v.source] || v.source)}</span><span class="set-ver-s">${esc(v.summary || "")}</span></span>
      ${v.revision === S.server.revision ? "" : `<button type="button" class="btn sm" data-action="settings-restore" data-n="${v.revision}">Restore</button>`}</li>`).join("");
  return `<p class="fine">Every Save is kept (the last 50). Restore puts a version on the page as a draft; Save then makes it current.</p>
    <ul class="set-vers">${rows || '<li class="fine">No versions yet.</li>'}</ul>`;
}

async function openVersions() {
  const body = $("settings-versions-body");
  body.innerHTML = '<div class="empty loading-state"><span class="spinner" aria-hidden="true"></span><span>Loading…</span></div>';
  openModal("settings-versions-modal");
  try {
    versions = await fetchVersions();
    body.innerHTML = versionsHtml(versions);
  } catch (e) {
    body.innerHTML = `<p class="set-msg">${esc(e.message)}</p>`;
  }
}

async function restoreVersion(n) {
  try {
    const doc = await fetchVersion(n);
    replaceDraft(`Replace the draft with revision ${n}?`, () => {
      applyDocument(doc);
      draftSource = "restore";
      closeModal("settings-versions-modal");
      renderAll();
      setNotes([{ level: "unknown", html: changeCount() ? `<b>Revision ${n} is on the page as a draft.</b> Review it, then Save to restore it.` : `<b>Revision ${n} is the same as the settings now.</b> There is nothing to restore.` }]);
      changed();
    });
  } catch (e) {
    addNote("crit", esc(e.message));
  }
}

async function openExport() {
  const text = $("settings-export-text");
  text.value = "Loading…";
  openModal("settings-export-modal");
  try { text.value = await fetchExport(); } catch (e) { text.value = e.message; }
}

function copyExport() {
  const text = $("settings-export-text");
  text.focus();
  text.select();
  try {
    if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text.value).catch(() => {});
    else document.execCommand("copy");
    showToast("Copied");
  } catch {
    showToast("Select the text and copy it", true);
  }
}

async function importText(name, text) {
  const msg = $("settings-import-msg");
  msg.textContent = "";
  try {
    const data = await importYaml(text);
    replaceDraft(`Replace the draft with ${name}?`, () => {
      applyDocument(data);
      draftSource = "yaml_import";
      closeModal("settings-import-modal");
      renderAll();
      setNotes([{ level: "unknown", html: `<b>Read ${esc(name)}.</b> It is a draft: review it, then Save.` }, ...(data.notes || []).map((t) => ({ level: "warn", html: esc(t) }))]);
      changed();
    });
  } catch (e) {
    msg.textContent = `The file was not imported. ${e.message}`;
  }
}

async function importFile(input) {
  const file = input.files && input.files[0];
  input.value = "";
  if (!file) return;
  $("settings-import-text").value = await file.text();
  $("settings-import-text").dataset.name = file.name;
}

// ---------- actions ----------
registerAction("settings-save", () => save());
registerAction("settings-cancel", () => discard());
registerAction("settings-reset", (el) => setValue(el.dataset.key, null));
registerAction("settings-choice", (el) => choose(el.dataset.key, el.dataset.arg));
registerAction("settings-goto", (el) => goto(el.dataset.arg));
registerAction("settings-driver", (el) => driverOp(el.dataset.key, el.dataset.op, el.dataset.name));
registerAction("settings-person", (el) => personOp(el.dataset.key, el.dataset.op, el.dataset.i));
registerAction("settings-versions", () => openVersions());
registerAction("settings-restore", (el) => restoreVersion(el.dataset.n));
registerAction("settings-export", () => openExport());
registerAction("settings-export-copy", () => copyExport());
registerAction("settings-import", () => { $("settings-import-msg").textContent = ""; openModal("settings-import-modal"); });
registerAction("settings-import-read", () => {
  const box = $("settings-import-text");
  if (!box.value.trim()) { $("settings-import-msg").textContent = "Choose a file or paste the YAML first."; return; }
  importText(box.dataset.name || "the pasted text", box.value);
});
registerAction("settings-confirm-yes", () => answer(true));
registerAction("settings-confirm-no", () => answer(false));
registerChange("settings-goto", (el) => goto(el.value));
registerChange("settings-import-file", (el) => importFile(el));

// The library source: the confirm explains what switching does, before the draft takes it.
function pickLibrary(el) {
  const key = el.dataset.key;
  const was = S.draft.values[key] ?? S.server.values[key].value;
  if (el.value === was) return;
  const value = el.value;
  el.value = was;
  ask({ title: "Switch the library source?", text: "Switching deletes no title, episode, watch or rating. Changes queued for the old source stay in the outbox and are not delivered while it is inactive; switching back delivers them (or lets you review them). It takes effect after a restart. See docs/drivers.md.", yes: "Use this source", no: "Keep the current one" },
    () => { setValue(key, value); el.value = value; }, () => el.focus());
}

function wirePane() {
  const root = pane();
  const onEdit = (ev) => {
    if (rendering) return;
    const el = ev.target;
    if (!(el instanceof Element)) return;
    if (el.matches("[data-secret-input]")) { secretInput(el); return; }
    if (el.matches("select[data-add]")) { if (ev.type === "change" && el.value) driverOp(el.dataset.add, "add", el.value); return; }
    if (el.matches("select[data-driver]")) { if (ev.type === "change") pickLibrary(el); return; }
    if (el.matches("[data-edit]") && el.closest(".set-row[data-key]")) edit(el);
  };
  root.addEventListener("input", onEdit);
  root.addEventListener("change", onEdit);
  wireWebhooks(root);
}

// Leaving the tab with a draft asks first: a click on another tab, a "g" shortcut or Back all end in a hashchange. This
// listener is registered before tabs.js's (mount runs first), stops it, and puts the address back until the answer.
window.addEventListener("hashchange", (ev) => {
  if (state.activeTab !== TAB || !changeCount()) return;
  const target = location.hash;
  if (/^#\/settings\b/.test(target)) return;
  ev.stopImmediatePropagation();
  history.replaceState(null, "", "#/settings");
  const n = changeCount();
  ask({ title: "Leave Settings?", text: `You have ${n} unsaved change${n === 1 ? "" : "s"}. Leaving drops them.`, yes: "Leave and discard", no: "Keep editing" },
    () => { clearDraft(); draftSource = ""; location.hash = target; });
});

// Closing the page with a draft: the browser asks.
window.addEventListener("beforeunload", (ev) => {
  if (changeCount()) { ev.preventDefault(); ev.returnValue = ""; }
});

onTabActivated(TAB, () => loadTab());

export default {
  id: "settings",
  mount() {
    wirePane();
    window.addEventListener("resize", stickJump);
  },
};
