// Write-only secret slots (the digest address, a webhook's address and signing secret). The server never sends a value,
// only {set, source, host}. The page can replace a slot (a password box whose text goes into the draft) or remove it.
// After a Save the box is empty again and the row shows "Set · host". A value typed here is only ever held in the draft
// and in its password box; it is never written into the page's text.

import { registerAction } from "../../js/dispatch.js";
import { esc } from "../../js/util.js";
import { S, setSecret } from "./store.js";

const editing = new Set(); // slots whose password box is open
const specs = new Map();   // slot -> the schema entry the row was built with

export const slotState = (slot) => (S.server && S.server.secrets[slot]) || { set: false };
export const slotId = (slot) => `set-slot-${slot.replace(/[^a-z0-9]+/gi, "-")}`;
export function resetEditing() { editing.clear(); }
export function rememberSlot(slot, spec) { specs.set(slot, spec); }

// Pinned: a variable, or (a webhook's signing secret) a file the item names. A spec marked `free` is a webhook secret
// whose item names neither any more (the draft stopped using them): the stored slot applies.
export const pinned = (slot, spec) => !(spec && spec.free) && ["env", "file"].includes(slotState(slot).source);
const freed = (slot, spec) => !!(spec && spec.free) && ["env", "file"].includes(slotState(slot).source);

function stateText(slot, spec) {
  const st = slotState(slot);
  if (slot in S.draft.secrets) return S.draft.secrets[slot] === "" ? "Will be removed (unsaved)" : "New value entered (unsaved)";
  if (freed(slot, spec)) return "Not set";
  if (st.source === "env") return `From environment${spec && spec.env ? ` · ${spec.env}` : ""}`;
  if (st.source === "file") return `From file${spec && spec.file ? ` · ${spec.file}` : ""}`;
  if (!st.set) return "Not set";
  return `Set${st.host ? ` · ${st.host}` : ""}`;
}

// The inside of a slot box.
export function slotInner(slot, spec) {
  const st = freed(slot, spec) ? { set: false } : slotState(slot);
  const fixed = pinned(slot, spec);
  const pending = slot in S.draft.secrets;
  const label = esc(spec.label);
  const attr = esc(slot);
  const open = editing.has(slot) && !fixed && !pending;
  const buttons = fixed || open ? "" : pending
    ? `<button type="button" class="btn sm" data-action="settings-secret-undo" data-slot="${attr}">Undo</button>`
    : `<button type="button" class="btn sm" data-action="settings-secret-replace" data-slot="${attr}">${st.set ? "Replace" : "Set"}</button>`
      + (st.set ? `<button type="button" class="btn sm danger" data-action="settings-secret-remove" data-slot="${attr}">Remove</button>` : "");
  const box = open
    ? `<div class="set-secret-edit"><input class="input" type="password" autocomplete="new-password" spellcheck="false" data-secret-input data-slot="${attr}" aria-label="New value of ${label}"${spec.kind === "url" ? ' placeholder="https://…"' : ""}>
        <button type="button" class="btn sm" data-action="settings-secret-cancel" data-slot="${attr}">Cancel</button></div>` : "";
  const note = fixed ? `<span class="set-ro">Read-only here: ${esc(spec.env || "the environment")} sets it.</span>` : "";
  return `<div class="set-main"><span class="set-label">${label}</span>
      <div class="set-input set-secret-state"><span class="chip${fixed ? " st st-unknown" : ""}" data-state>${fixed ? '<svg class="i" aria-hidden="true"><use href="#i-lock"/></svg>' : ""}${esc(stateText(slot, spec))}</span>${buttons}</div></div>
    ${box}${note}<p class="set-msg" data-msg role="alert">${esc(S.errors[`secrets.${slot}`] || "")}</p>`;
}

export function slotHtml(slot, spec) {
  rememberSlot(slot, spec);
  return `<div class="set-row set-slot" data-slot="${esc(slot)}" id="${slotId(slot)}">${slotInner(slot, spec)}</div>`;
}

function redraw(slot, focus = true) {
  document.querySelectorAll(`.set-slot[data-slot="${CSS.escape(slot)}"]`).forEach((box) => {
    box.innerHTML = slotInner(slot, specs.get(slot) || { label: slot, kind: "token" });
    if (focus) (box.querySelector("input") || box.querySelector("button"))?.focus();
  });
}

export function refreshSlots() {
  document.querySelectorAll(".set-slot[data-slot]").forEach((box) => {
    const input = box.querySelector("input");
    if (input && document.activeElement === input) return; // being typed in
    box.innerHTML = slotInner(box.dataset.slot, specs.get(box.dataset.slot) || { label: box.dataset.slot, kind: "token" });
  });
}

function dropPending(slot) {
  editing.delete(slot);
  setSecret(slot, undefined);
  redraw(slot);
}

registerAction("settings-secret-replace", (el) => { editing.add(el.dataset.slot); redraw(el.dataset.slot); });
registerAction("settings-secret-cancel", (el) => dropPending(el.dataset.slot));
registerAction("settings-secret-undo", (el) => dropPending(el.dataset.slot));
registerAction("settings-secret-remove", (el) => { editing.delete(el.dataset.slot); setSecret(el.dataset.slot, ""); redraw(el.dataset.slot); });

// Typing in a slot's box: the text goes into the draft (memory only; it is shown nowhere).
export function secretInput(el) {
  const slot = el.dataset.slot;
  const text = el.value;
  if (text === "") setSecret(slot, undefined); else setSecret(slot, text);
  const state = el.closest(".set-slot")?.querySelector("[data-state]");
  if (state) state.textContent = text === "" ? stateText(slot, specs.get(slot)) : "New value entered (unsaved)";
}
