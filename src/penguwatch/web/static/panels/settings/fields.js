// One setting as a row: label, input, unit, and under it the effect, the default, the chips and "Reset to default".
// Inputs are built from the schema (type, range, choices). Every row's default is put into words by one function,
// `defaultText`; the chips are "Changed", "Unsaved", "From environment", "Applies after a restart".

import { esc, icon } from "../../js/util.js";
import { fmt, isNumeric, namesProblem, parse, problem, same } from "./model.js";
import { S, changed, inDraft, isEnv, setValue, valueOf, valueState } from "./store.js";

const idOf = (key) => `set-in-${key.replace(/[^a-z0-9]+/gi, "-")}`;

export const driverLabel = (name) => (S.schema.drivers.find((d) => d.name === name) || { label: name }).label;
const roleOf = (spec) => (S.schema.roles || []).find((r) => `drivers.${r.role}` === spec.key);
const offering = (spec) => {
  const role = roleOf(spec);
  return S.schema.drivers.filter((d) => !role || d.offers.includes(role.capability));
};
const words = (spec, v) => fmt(spec, v, driverLabel);

// ---------- time zones: this browser's first ----------
function browserZone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch { return ""; }
}

function zoneOptions(spec, current) {
  let all = [];
  try { all = Intl.supportedValuesOf("timeZone"); } catch { all = []; }
  const mine = browserZone();
  const seen = new Set(["", mine, current]);
  const opt = (z, label) => `<option value="${esc(z)}"${z === current ? " selected" : ""}>${esc(label ?? z)}</option>`;
  const top = [opt("", "Empty: the server's own zone")];
  if (mine) top.push(opt(mine, `${mine} (this browser)`));
  if (current && current !== mine) top.push(opt(current));
  const rest = all.filter((z) => !seen.has(z)).map((z) => opt(z));
  return `<optgroup label="Suggested">${top.join("")}</optgroup><optgroup label="All zones">${rest.join("")}</optgroup>`;
}

// ---------- the controls ----------
function pickerHtml(spec, value, env) {
  const chosen = Array.isArray(value) ? value : [];
  const rest = offering(spec).filter((d) => !chosen.includes(d.name));
  const off = env ? " disabled" : "";
  const rows = chosen.map((name, i) => `<li><span>${esc(driverLabel(name))}</span>
      <button type="button" class="btn icon sm" data-action="settings-driver" data-key="${esc(spec.key)}" data-op="up" data-name="${esc(name)}" aria-label="Move ${esc(driverLabel(name))} up"${i === 0 || env ? " disabled" : ""}>${icon("up")}</button>
      <button type="button" class="btn icon sm set-down" data-action="settings-driver" data-key="${esc(spec.key)}" data-op="down" data-name="${esc(name)}" aria-label="Move ${esc(driverLabel(name))} down"${i === chosen.length - 1 || env ? " disabled" : ""}>${icon("up")}</button>
      <button type="button" class="btn sm danger" data-action="settings-driver" data-key="${esc(spec.key)}" data-op="remove" data-name="${esc(name)}" aria-label="Remove ${esc(driverLabel(name))}"${off}>Remove</button></li>`).join("");
  const add = rest.length ? `<span class="set-add"><label class="sr-only" for="${idOf(spec.key)}-add">Add a source to ${esc(spec.label)}</label>
      <select class="input" id="${idOf(spec.key)}-add" data-add="${esc(spec.key)}"${off}><option value="">Add a source…</option>${rest.map((d) => `<option value="${esc(d.name)}">${esc(d.label)}</option>`).join("")}</select></span>` : "";
  return `<div class="set-picker" id="${idOf(spec.key)}"><ol class="set-order">${rows || `<li class="fine">${esc(spec.empty || "None")}</li>`}</ol>${add}</div>`;
}

function controlHtml(spec, id) {
  const value = valueOf(spec.key);
  const env = isEnv(spec.key);
  const common = `id="${id}" data-key="${esc(spec.key)}" data-edit`;
  const label = `aria-labelledby="${id}-l"`;
  switch (spec.type) {
    case "boolean":
      return `<label class="check"><input type="checkbox" ${common}${value ? " checked" : ""}${env ? " disabled" : ""} ${label}><span>${value ? "On" : "Off"}</span></label>`;
    case "choice": {
      const choices = spec.choices || [];
      const name = (c) => (spec.choice_labels && spec.choice_labels[c]) || String(c);
      if (choices.length <= 3) {
        return `<span class="seg" role="group" ${label} id="${id}">${choices.map((c) => `<button type="button" data-action="settings-choice" data-key="${esc(spec.key)}" data-arg="${esc(c)}" aria-pressed="${String(c) === String(value)}"${env ? " disabled" : ""}>${esc(name(c))}</button>`).join("")}</span>`;
      }
      return `<select class="input" ${common}${env ? " disabled" : ""}>${choices.map((c) => `<option value="${esc(c)}"${String(c) === String(value) ? " selected" : ""}>${esc(name(c))}</option>`).join("")}</select>`;
    }
    case "timezone":
      return `<select class="input" ${common}${env ? " disabled" : ""}>${zoneOptions(spec, value)}</select>`;
    case "theme": {
      const names = spec.choice_labels || {};
      const ids = spec.choices || Object.keys(names);
      const known = ids.includes(value) ? ids : [...ids, value];
      return `<select class="input" ${common}${env ? " disabled" : ""}>${known.map((t) => `<option value="${esc(t)}"${t === value ? " selected" : ""}>${esc(names[t] || `${t} (not found)`)}</option>`).join("")}</select>`;
    }
    case "hour":
      return `<select class="input" ${common}${env ? " disabled" : ""}>${Array.from({ length: 24 }, (_, h) => `<option value="${h}"${Number(value) === h ? " selected" : ""}>${String(h).padStart(2, "0")}:00</option>`).join("")}</select>`;
    case "names":
      return peopleHtml(spec, id, env);
    case "driver":
      return `<select class="input" ${common} data-driver${env ? " disabled" : ""}>${offering(spec).map((d) => `<option value="${esc(d.name)}"${d.name === value ? " selected" : ""}>${esc(d.label)}</option>`).join("")}</select>`;
    case "drivers":
      return pickerHtml(spec, value, env);
    default: {
      const ro = env ? " readonly aria-readonly=\"true\"" : "";
      const num = isNumeric(spec);
      return `<input class="input${num ? " num" : ""}" type="text" ${num ? "inputmode=\"decimal\" " : ""}autocomplete="off" spellcheck="false" ${common}${ro} value="${esc(value ?? "")}"${spec.max_length ? ` maxlength="${spec.max_length}"` : ""} placeholder="${esc(spec.empty || "")}" aria-describedby="${id}-m">`;
    }
  }
}

// ---------- the household's people: a row each (name, Remove), then "Add person" ----------
// While what is typed is refused (an empty or repeated name, a new row not named yet) the rows on screen are kept here,
// so a redraw does not lose them; otherwise the rows are the value (the draft's, else the server's).
const peopleRows = {};

const personOf = (p) => (p && typeof p === "object" ? { ...(p.id ? { id: p.id } : {}), name: p.name ?? "" } : { name: String(p ?? "") });

function peopleNow(spec) {
  if (S.invalid[spec.key] && peopleRows[spec.key]) return peopleRows[spec.key];
  const value = valueOf(spec.key);
  return (Array.isArray(value) ? value : []).map(personOf);
}

function peopleHtml(spec, id, env) {
  const people = peopleNow(spec);
  const max = spec.max_length || 20;
  const rows = people.map((p, i) => `<li class="set-person">
      <input class="input" type="text" autocomplete="off" data-key="${esc(spec.key)}" data-edit data-i="${i}" data-member="${esc(p.id || "")}" maxlength="40" value="${esc(p.name)}" aria-label="Person ${i + 1}"${env ? " readonly" : ""}>
      <button type="button" class="btn sm danger" data-action="settings-person" data-key="${esc(spec.key)}" data-op="remove" data-i="${i}" aria-label="Remove ${esc(p.name || `person ${i + 1}`)}"${people.length <= 1 || env ? " disabled" : ""}>Remove</button>
    </li>`).join("");
  const add = `<button type="button" class="btn sm outline set-person-add" data-action="settings-person" data-key="${esc(spec.key)}" data-op="add"${people.length >= max || env ? " disabled" : ""}>${icon("plus")}Add person</button>`;
  return `<div class="set-people" id="${id}"><ol class="set-people-list">${rows}</ol>${add}<span class="fine set-people-n">${people.length} of at most ${max}</span></div>`;
}

// The rows on screen as people: [{id?, name}] (a row without an id is someone new).
function readPeople(row) {
  return [...row.querySelectorAll("input[data-i]")].map((i) => ({ ...(i.dataset.member ? { id: i.dataset.member } : {}), name: i.value.trim() }));
}

// The rows as the value: accepted (and put in the draft), or kept on screen with the reason.
function takePeople(spec, people) {
  const why = namesProblem(spec, people);
  if (why) {
    peopleRows[spec.key] = people;
    S.invalid[spec.key] = why;
    changed();
    return;
  }
  delete peopleRows[spec.key];
  setValue(spec.key, people);
}

// "Add person" (a new, empty row: named before it can be saved) and "Remove" (never the last one). A removed person's
// titles lose them when the change is saved; a title left with nobody goes to the first person (the server says how
// many before the save).
export function personOp(key, op, index) {
  const spec = S.spec[key];
  if (!spec || isEnv(key)) return;
  const row = document.getElementById(`${idOf(key)}-row`);
  if (!row) return;
  // a name being typed is taken first: redrawing the rows removes its input, and the browser then sends that input's
  // change event (read from the old rows) after this change
  if (row.contains(document.activeElement)) document.activeElement.blur();
  const people = readPeople(row);
  if (op === "add" && people.length < (spec.max_length || 20)) people.push({ name: "" });
  if (op === "remove" && people.length > 1) people.splice(Number(index), 1);
  takePeople(spec, people);
  rebuildControl(row, spec);
  const inputs = row.querySelectorAll("input[data-i]");
  if (op === "add") inputs[inputs.length - 1]?.focus();
  else (inputs[Math.min(Number(index), inputs.length - 1)] || row.querySelector(".set-person-add"))?.focus();
}

// ---------- the line under the input ----------
export function defaultText(spec) {
  if (spec.type === "timezone") return "Default: empty, the server's own zone";
  return `Default: ${words(spec, spec.default)}`;
}

function chipsHtml(spec) {
  const st = valueState(spec.key);
  const chips = [];
  if (isEnv(spec.key)) {
    chips.push(`<span class="chip st st-unknown" data-badge="env">${icon("lock")}From environment · ${esc(spec.env || "")}</span>`);
  } else {
    const v = inDraft(spec.key) ? S.draft.values[spec.key] : (st.stored ?? null);
    if (v !== null && !same(v, spec.default)) chips.push('<span class="chip st st-busy" data-badge="changed">Changed</span>');
    if (inDraft(spec.key)) chips.push('<span class="chip st st-warn" data-badge="unsaved">Unsaved</span>');
  }
  // env_ignored: the server's sentence, naming the variable and why, never its value ("PENGUWATCH_LOG_LEVEL is set but
  // not valid (...): ignored")
  if (st.env_ignored) {
    chips.push(`<span class="chip st st-warn" data-badge="env-invalid">${icon("warn")}${esc(String(st.env_ignored))}</span>`);
  }
  if (!spec.live) chips.push('<span class="chip st st-unknown" data-badge="restart-role">Applies after a restart</span>');
  if (st.pending_restart) chips.push('<span class="chip st st-warn" data-badge="restart">Restart needed to apply the saved value</span>');
  if (st.live_failed) chips.push('<span class="chip st st-crit" data-badge="live-failed">Saved; applies at the next start</span>');
  return chips.join(" ");
}

export function metaHtml(spec) {
  const st = valueState(spec.key);
  const v = inDraft(spec.key) ? S.draft.values[spec.key] : (st.stored ?? null);
  const showReset = !isEnv(spec.key) && v !== null && !same(v, spec.default);
  const reset = showReset ? `<button type="button" class="set-lnk" data-action="settings-reset" data-key="${esc(spec.key)}">Reset to default</button>` : "";
  const warn = S.warnings.filter((w) => w.where === `values.${spec.key}`).map((w) => `<span class="set-warn">${esc(w.message)}</span>`).join("");
  const lock = isEnv(spec.key) ? `<span class="set-ro">Read-only here: ${esc(spec.env || "the environment")} sets it. Remove the variable and restart to edit it on this page.</span>` : "";
  return `<span class="set-effect">${esc(spec.effect)}</span><span class="set-side"><span class="set-def">${esc(defaultText(spec))}</span>${chipsHtml(spec)}${reset}</span>${lock}${warn}`;
}

export const messageText = (key) => S.invalid[key] || S.errors[`values.${key}`] || "";

export function rowHtml(spec) {
  const id = idOf(spec.key);
  const unit = isNumeric(spec) && spec.type !== "hour" && spec.unit ? `<span class="set-unit">${esc(spec.unit)}</span>` : "";
  return `<div class="set-row" data-key="${esc(spec.key)}" data-type="${esc(spec.type)}" id="${id}-row">
    <div class="set-main">
      <label class="set-label" id="${id}-l" for="${id}">${esc(spec.label)}</label>
      <div class="set-input" data-control>${controlHtml(spec, id)}${unit}</div>
    </div>
    <div class="set-meta" id="${id}-m" data-meta>${metaHtml(spec)}</div>
    <p class="set-msg" data-msg role="alert">${esc(messageText(spec.key))}</p>
  </div>`;
}

// ---------- reading and editing ----------
function readNames(row, spec) {
  const people = readPeople(row);
  const why = namesProblem(spec, people);
  if (why) peopleRows[spec.key] = people;
  else delete peopleRows[spec.key];
  return why ? { problem: why } : { stored: people };
}

// The input of `el` read as a stored value: {stored} or {problem}.
export function readInput(el, spec) {
  if (spec.type === "boolean") return { stored: el.checked };
  if (spec.type === "names") return readNames(el.closest(".set-row"), spec);
  if (spec.type === "hour") return { stored: Number(el.value) };
  if (spec.type === "choice") {
    const c = (spec.choices || []).find((x) => String(x) === el.value);
    return c === undefined ? { problem: "Pick one of the choices." } : { stored: c };
  }
  if (spec.type === "text" || spec.type === "number" || spec.type === "integer") {
    const why = problem(spec, el.value);
    if (why) return { problem: why };
    return { stored: spec.type === "text" ? el.value.trim() : parse(spec, el.value) };
  }
  return { stored: el.value };
}

// An edit of a row's input (typing, switching, picking).
export function edit(el) {
  const key = el.dataset.key;
  const spec = S.spec[key];
  if (!spec || isEnv(key)) return;
  const got = readInput(el, spec);
  if (got.problem) {
    S.invalid[key] = got.problem;
    changed();   // the rows, the Save button (off while a box is refused) and the validation timer
    return;
  }
  setValue(key, got.stored);
}

export function choose(key, choice) {
  const spec = S.spec[key];
  if (!spec || isEnv(key)) return;
  const c = (spec.choices || []).find((x) => String(x) === String(choice));
  setValue(key, c === undefined ? choice : c);
}

// Ordered pickers (drivers.catalog / schedule / discovery): up, down, remove, add.
export function driverOp(key, op, name) {
  const spec = S.spec[key];
  if (!spec || isEnv(key)) return;
  const list = [...(valueOf(key) || [])];
  const i = list.indexOf(name);
  if (op === "remove" && i >= 0) list.splice(i, 1);
  if (op === "add" && i < 0) list.push(name);
  if ((op === "up" && i > 0) || (op === "down" && i >= 0 && i < list.length - 1)) {
    const j = op === "up" ? i - 1 : i + 1;
    [list[i], list[j]] = [list[j], list[i]];
  }
  const min = spec.min_length || 0;
  if (list.length < min) { S.invalid[key] = `At least ${min} source${min === 1 ? "" : "s"} is needed.`; changed(); return; }
  setValue(key, list);
  const row = document.getElementById(`${idOf(key)}-row`);
  rebuildControl(row, spec);
  const again = row?.querySelector(`[data-op="${op}"][data-name="${CSS.escape(name)}"]:not([disabled])`);
  (again || row?.querySelector("[data-op]:not([disabled]), select"))?.focus();
}

function rebuildControl(row, spec) {
  if (!row) return;
  const box = row.querySelector("[data-control]");
  box._html = controlHtml(spec, idOf(spec.key));
  box.innerHTML = box._html;
}

// Redraws a meta line only when its content changed, and keeps focus on its "Reset" if that had it.
function paintMeta(meta, html) {
  if (meta._html === html) return;
  const had = meta.contains(document.activeElement) ? document.activeElement.dataset.action : null;
  meta.innerHTML = html;
  meta._html = html;
  if (had) meta.querySelector(`[data-action="${had}"]`)?.focus();
}

// Brings everything that depends on state up to date: every meta line, message and the inputs not being typed in.
export function refreshRows(root = document.getElementById("settings-tab")) {
  root.querySelectorAll(".set-row[data-key]").forEach((row) => {
    const spec = S.spec[row.dataset.key];
    if (!spec) return;
    paintMeta(row.querySelector("[data-meta]"), metaHtml(spec));
    row.querySelector("[data-msg]").textContent = messageText(spec.key);
    const bad = !!messageText(spec.key);
    row.classList.toggle("bad", bad);
    row.classList.toggle("unsaved", inDraft(spec.key));
    const input = row.querySelector("input[data-key], select[data-key]");
    if (input) input.setAttribute("aria-invalid", String(bad));
    const value = valueOf(spec.key);
    const typing = row.contains(document.activeElement) && document.activeElement.matches("input[type=text]");
    if (!input || typing || bad) return;
    if (spec.type === "boolean") { input.checked = !!value; input.nextElementSibling.textContent = value ? "On" : "Off"; }
    else if (spec.type === "names") {
      // redrawn when the rows on screen are not the value (a reset, a restored version, a discarded draft); never
      // while one of them has the focus, and never when they already match (a blur's change event lands here too)
      if (!same(readPeople(row), peopleNow(spec)) && !row.contains(document.activeElement)) rebuildControl(row, spec);
    } else if (spec.type === "drivers") { /* rebuilt by driverOp */ }
    else if (spec.type === "hour") input.value = String(value);
    else if (spec.type === "number" || spec.type === "integer") { if (parse(spec, input.value) !== value) input.value = String(value ?? ""); }
    else if (input.value !== String(value ?? "")) input.value = value ?? "";
  });
  root.querySelectorAll("[data-action=settings-choice]").forEach((b) => {
    b.setAttribute("aria-pressed", String(String(valueOf(b.dataset.key)) === b.dataset.arg));
  });
  root.querySelectorAll(".set-row[data-type=drivers]").forEach((row) => {
    const spec = S.spec[row.dataset.key];
    const html = controlHtml(spec, idOf(spec.key));
    const box = row.querySelector("[data-control]");
    if (box._html !== html && !row.contains(document.activeElement)) { box.innerHTML = html; box._html = html; }
  });
}
