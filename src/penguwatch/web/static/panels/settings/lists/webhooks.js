// The event webhooks list: cards (name, events, address, signing secret). The address and the
// signing secret are write-only slots (secrets.js): "Set · host", Replace, Remove; the page never reads them back. A new
// webhook (an id the server does not have) needs its address in the same save, so Save stays off until it has one.
// The page never sets a `secret_env` / `secret_file` (the server refuses a new or changed one). A hook
// imported from config.yaml with one shows it read-only, with "Stop using it" (clears both; the hook then uses a stored
// signing secret, which can be set in the same save). A new hook has no such fields.

import { api } from "../../../js/api.js";
import { registerAction } from "../../../js/dispatch.js";
import { esc, icon, showToast } from "../../../js/util.js";
import { webhookId } from "../model.js";
import { rememberSlot, slotHtml, slotState } from "../secrets.js";
import { S, listItems, listState, setList, setSecret } from "../store.js";

const NAME = "webhooks";
const urlSlot = (id) => `webhook.${id}.url`;
const secretSlot = (id) => `webhook.${id}.secret`;

let box = null;
let items = [];
let events = {};          // event name -> description (GET /api/webhooks/events)
const open = new Set();   // ids of expanded cards
let problems = {};        // "0.url" -> message (the server's answers for this list)

const saved = (id) => listState(NAME).items.some((i) => i.id === id);
const spec = () => S.schema.lists[NAME];
const maxItems = () => (spec() && spec().max_items) || 10;

function hasUrl(id) {
  if (urlSlot(id) in S.draft.secrets) return S.draft.secrets[urlSlot(id)] !== "";
  return !!slotState(urlSlot(id)).set;
}

// What keeps Save off: a nameless webhook, one with no event, a new one with no address.
function localProblems() {
  const found = {};
  items.forEach((item) => {
    if (!String(item.name || "").trim()) found[`webhook-name:${item.id}`] = "Give the webhook a name.";
    else if (!(item.events || []).length) found[`webhook-events:${item.id}`] = "Pick at least one event.";
    else if (!saved(item.id) && !hasUrl(item.id)) found[`webhook-url:${item.id}`] = "Address needed.";
  });
  return found;
}

export function syncProblems() {
  Object.keys(S.invalid).filter((k) => k.startsWith("webhook-")).forEach((k) => delete S.invalid[k]);
  if (NAME in S.draft.lists) Object.assign(S.invalid, localProblems());
  if (!box) return;
  box.querySelectorAll(".set-card").forEach((card) => {
    const item = items[Number(card.dataset.i)];
    if (!item) return;
    const sum = card.querySelector("summary");
    const chip = sum.querySelector("[data-chip=url]");
    const needs = !hasUrl(item.id);
    if (needs && !chip) sum.insertAdjacentHTML("beforeend", '<span class="chip st st-crit" data-chip="url">Address needed</span>');
    if (!needs && chip) chip.remove();
    const msg = card.querySelector("[data-card-msg]");
    if (msg) msg.textContent = [S.invalid[`webhook-name:${item.id}`], S.invalid[`webhook-events:${item.id}`], S.invalid[`webhook-url:${item.id}`], ...cardErrors(card.dataset.i)].filter(Boolean).join(" ");
  });
}

const cardErrors = (i) => Object.entries(problems).filter(([k]) => k.startsWith(`${i}.`)).map(([, m]) => m);

function stateChip(item) {
  const st = (listState(NAME).states || {})[item.id];
  if (!st || !saved(item.id)) return "";
  if (st.state === "address_needed") return '<span class="chip st st-warn" data-chip="state">Off: address needed</span>';
  if (st.state === "disabled") return `<span class="chip st st-crit" data-chip="state">Off${st.reason ? `: ${esc(st.reason)}` : ""}</span>`;
  return "";
}

function secretSpec(item) {
  const label = "Signing secret";
  if (item.secret_env) return { label, kind: "token", env: item.secret_env };
  if (item.secret_file) return { label, kind: "token", file: item.secret_file };
  // `free`: the item names no variable or file (any more), so the stored slot applies even while the server still
  // reports the imported source (until the save that stops using it)
  return { label, kind: "token", env: null, free: true };
}

// An imported variable or file: shown read-only, with the one change the page may make to it.
function importedHtml(item) {
  if (!item.secret_env && !item.secret_file) return "";
  const what = item.secret_env ? `the environment variable <code>${esc(item.secret_env)}</code>` : `the file <code>${esc(item.secret_file)}</code>`;
  return `<div class="set-f set-imported" data-imported><p class="fine">The signing secret is read from ${what} (from config.yaml). It can't be changed here; stop using it to set a stored signing secret instead.</p>
      <div><button type="button" class="btn sm outline" data-action="settings-webhook-stop-import" data-id="${esc(item.id)}">Stop using it</button></div></div>`;
}

function cardHtml(item, i) {
  const id = item.id;
  const names = Object.keys(events);
  const chosen = new Set(item.events || []);
  const evs = names.map((e) => `<label class="check"><input type="checkbox" data-wf="events" data-ev="${esc(e)}"${chosen.has(e) ? " checked" : ""}><span>${esc(e)}</span></label>`).join("");
  const dirty = !saved(id) || (NAME in S.draft.lists);
  const canTest = saved(id) && !dirty && !(urlSlot(id) in S.draft.secrets) && !(secretSlot(id) in S.draft.secrets) && ((listState(NAME).states || {})[id] || {}).state === "ok";
  rememberSlot(urlSlot(id), { label: "Address (URL)", kind: "url", env: null });
  return `<details class="set-card" data-i="${i}" data-id="${esc(id)}"${open.has(id) ? " open" : ""}>
    <summary>${icon("chevron", "set-chev")}<span class="set-card-t">${esc(item.name || "New webhook")}</span><span class="set-card-s">${(item.events || []).length} event${(item.events || []).length === 1 ? "" : "s"}${hasUrl(id) ? "" : " · address needed"}</span>${stateChip(item)}${hasUrl(id) ? "" : '<span class="chip st st-crit" data-chip="url">Address needed</span>'}</summary>
    <div class="set-card-b">
      <div class="set-f"><label for="wf-${i}-name">Name</label><input class="input" id="wf-${i}-name" type="text" autocomplete="off" data-wf="name" maxlength="60" value="${esc(item.name || "")}"></div>
      <fieldset class="set-events"><legend>Events</legend><div class="set-checks">${evs || '<span class="fine">Loading the event names…</span>'}</div></fieldset>
      ${slotHtml(urlSlot(id), { label: "Address (URL)", kind: "url", env: null })}
      ${slotHtml(secretSlot(id), secretSpec(item))}
      ${importedHtml(item)}
      <p class="set-msg" data-card-msg role="alert"></p>
      <div class="set-card-f">
        ${canTest ? `<button type="button" class="btn sm outline" data-action="settings-webhook-test" data-name="${esc(item.name)}">Send test</button>` : ""}
        <button type="button" class="btn sm danger" data-action="settings-webhook-remove" data-id="${esc(id)}">${icon("close")}Remove</button>
      </div>
    </div></details>`;
}

export function renderWebhooks() {
  if (!box || !S.schema) return;
  items = listItems(NAME);
  const addDisabled = items.length >= maxItems() ? " disabled" : "";
  box.innerHTML = `<div class="set-list" data-list="${NAME}">
    <h4 class="set-group-t">${esc(spec().label)}</h4>
    <p class="fine">Called with a signed message when a title is added, a status changes or an episode is watched. The address and the signing secret are write-only: you can replace or remove them, but they are never shown again or exported.</p>
    ${items.length ? items.map(cardHtml).join("") : '<p class="fine set-empty">No webhooks yet.</p>'}
    <div><button type="button" class="btn sm outline" data-action="settings-webhook-add"${addDisabled}>${icon("plus")}<span>Add a webhook</span></button>${addDisabled ? ` <span class="fine">At most ${maxItems()}.</span>` : ""}</div></div>`;
  syncProblems();
}

export function mountWebhooks(container) {
  box = container;
  open.clear();
  problems = {};
  renderWebhooks();
}

// The server's answers for this list, keyed "<index>.<field>".
export function webhookProblems(byWhere) {
  problems = byWhere || {};
  syncProblems();
  box?.querySelectorAll(".set-card").forEach((c) => { if (cardErrors(c.dataset.i).length) c.open = true; });
}

export const loadEvents = async () => {
  try { events = await fetchEventNames(); } catch { events = {}; }
  renderWebhooks();
};

async function fetchEventNames() {
  const data = await api("/api/webhooks/events");
  return data.events || {};
}

// ---------- edits ----------
const commit = () => setList(NAME, items);

function addItem() {
  if (items.length >= maxItems()) return;
  const id = webhookId("New webhook", items.map((i) => i.id));
  items.push({ id, name: "New webhook", events: Object.keys(events), secret_env: null, secret_file: null });
  open.add(id);
  commit();
  renderWebhooks();
  box.querySelector(`.set-card[data-id="${CSS.escape(id)}"] [data-wf=name]`)?.select();
}

function stopImported(id) {
  const item = items.find((i) => i.id === id);
  if (!item) return;
  item.secret_env = null;
  item.secret_file = null;
  open.add(id);
  commit();
  renderWebhooks();
}

function removeItem(id) {
  items = items.filter((i) => i.id !== id);
  open.delete(id);
  setSecret(urlSlot(id), undefined);
  setSecret(secretSlot(id), undefined);
  commit();
  renderWebhooks();
}

// A new webhook's id follows its name (its slots move with it); a saved one's id never changes.
function rename(item) {
  if (saved(item.id)) return false;
  const next = webhookId(item.name, items.filter((o) => o !== item).map((o) => o.id));
  if (next === item.id) return false;
  for (const make of [urlSlot, secretSlot]) {
    const old = make(item.id);
    if (old in S.draft.secrets) { const v = S.draft.secrets[old]; delete S.draft.secrets[old]; S.draft.secrets[make(next)] = v; }
  }
  if (open.delete(item.id)) open.add(next);
  item.id = next;
  return true;
}

function edited(ev) {
  const el = ev.target instanceof Element ? ev.target.closest("[data-wf]") : null;
  if (!el || !box?.contains(el)) return;
  const card = el.closest(".set-card");
  const item = items[Number(card.dataset.i)];
  if (!item) return;
  const f = el.dataset.wf;
  if (f === "events") item.events = [...card.querySelectorAll("[data-ev]")].filter((c) => c.checked).map((c) => c.dataset.ev);
  else item[f] = el.value.trim() === "" && f !== "name" ? null : el.value;
  card.querySelector(".set-card-t").textContent = item.name || "New webhook";
  commit();
  if (f === "name" && ev.type === "change" && rename(item)) { commit(); renderWebhooks(); }
}

async function testHook(el) {
  el.disabled = true;
  try {
    const r = await api(`/api/webhooks/${encodeURIComponent(el.dataset.name)}/test`, { method: "POST" });
    showToast(r.delivered ? "Test event delivered" : `Test event failed: ${r.error || "no answer"}`, !r.delivered);
  } catch (e) {
    showToast(e.message, true);
  }
  el.disabled = false;
}

export function wireWebhooks(pane) {
  pane.addEventListener("input", edited);
  pane.addEventListener("change", edited);
  pane.addEventListener("toggle", (ev) => {
    const card = ev.target;
    if (!(card instanceof Element) || !card.classList.contains("set-card")) return;
    if (card.open) open.add(card.dataset.id); else open.delete(card.dataset.id);
  }, true);
}

registerAction("settings-webhook-add", addItem);
registerAction("settings-webhook-remove", (el) => removeItem(el.dataset.id));
registerAction("settings-webhook-stop-import", (el) => stopImported(el.dataset.id));
registerAction("settings-webhook-test", testHook);
