// What the Settings tab knows: the schema, the server's state at the revision it loaded, and the draft, a change against
// that state. Nothing is sent from the draft functions; save() and validate() at the bottom do the talking. The draft is
//   { values: {key: value | null (reset)}, lists: {name: items | null (reset)}, secrets: {slot: text | "" (remove)} }

import { clone, same, sameItems } from "./model.js";

export const emptyDraft = () => ({ values: {}, lists: {}, secrets: {} });

export const S = {
  schema: null,
  spec: {},       // key -> setting spec
  server: null,   // GET /api/settings
  draft: emptyDraft(),
  invalid: {},    // key -> message: what is typed and not acceptable (the draft keeps the last acceptable value)
  errors: {},     // where -> message, from the last POST /api/settings/validate (or a refused save)
  warnings: [],   // [{where, message}] from the same answer
  summary: "",
};

const listeners = new Set();
// fn() runs after every edit of the draft (the Save label, the validation timer, the rows).
export const watchDraft = (fn) => { listeners.add(fn); };
export const changed = () => listeners.forEach((fn) => fn());

export function load(schema, server) {
  S.schema = schema;
  S.spec = Object.fromEntries(schema.settings.map((s) => [s.key, s]));
  S.server = server;
}

export function clearDraft() {
  S.draft = emptyDraft();
  S.invalid = {};
  S.errors = {};
  S.warnings = [];
  S.summary = "";
}

export const valueState = (key) => (S.server && S.server.values[key]) || { value: null, stored: null, source: "default" };
export const isEnv = (key) => valueState(key).source === "env";
export const inDraft = (key) => key in S.draft.values;

// The value the row shows: the draft's, else the server's effective value.
export function valueOf(key) {
  if (inDraft(key)) return S.draft.values[key] === null ? S.spec[key].default : S.draft.values[key];
  return valueState(key).value;
}

// Puts `v` (null: reset to the default) into the draft, or takes the key out when it is no change.
export function setValue(key, v) {
  const spec = S.spec[key];
  if (!spec || isEnv(key)) return;
  const stored = valueState(key).stored ?? null;
  if (v === null || same(v, spec.default)) {
    if (stored === null) delete S.draft.values[key]; else S.draft.values[key] = null;
  } else if (same(v, stored)) {
    delete S.draft.values[key];
  } else {
    S.draft.values[key] = v;
  }
  delete S.invalid[key];
  changed();
}

export const listState = (name) => (S.server && S.server.lists[name]) || { set: false, items: [], states: {} };

// The items of a list as the page shows them: the draft's, else the server's (the defaults while it is not set).
export function listItems(name) {
  if (name in S.draft.lists) return clone(S.draft.lists[name] === null ? S.schema.lists[name].default_items : S.draft.lists[name]);
  return clone(listState(name).items);
}

export function setList(name, items) {
  const srv = listState(name);
  const defaults = S.schema.lists[name].default_items;
  if (items === null || sameItems(items, defaults)) {
    if (srv.set) S.draft.lists[name] = null; else delete S.draft.lists[name];
  } else if (srv.set && sameItems(items, srv.items)) {
    delete S.draft.lists[name];
  } else {
    S.draft.lists[name] = clone(items);
  }
  changed();
}

export function setSecret(slot, text) {
  if (text === undefined) delete S.draft.secrets[slot]; else S.draft.secrets[slot] = text;
  changed();
}

export const changeCount = () => Object.keys(S.draft.values).length + Object.keys(S.draft.lists).length + Object.keys(S.draft.secrets).length;
export const hasProblems = () => Object.keys(S.invalid).length > 0 || Object.keys(S.errors).length > 0;

// A document {values, lists} (a version, an imported file) as a draft against the loaded state: only what differs from it.
// A key the document does not have goes back to its default. Keys pinned by the environment are left alone, and so are
// the ones in `doc.kept` (the server refused the file's value: the current one stays).
export function applyDocument(doc) {
  clearDraft();
  const values = doc.values || {};
  const kept = new Set(doc.kept || []);
  for (const key of Object.keys(S.spec)) {
    if (isEnv(key) || kept.has(key)) continue;
    quietValue(key, key in values, values[key]);
  }
  for (const name of Object.keys(S.schema.lists)) {
    const items = doc.lists ? doc.lists[name] : undefined;
    const srv = listState(name);
    if (items === undefined || items === null) { if (srv.set) S.draft.lists[name] = null; } else if (!srv.set || !sameItems(items, srv.items)) S.draft.lists[name] = clone(items);
  }
  changed();
}

function quietValue(key, present, v) {
  const spec = S.spec[key];
  const stored = valueState(key).stored ?? null;
  if (!present || v === null) {
    if (stored !== null) S.draft.values[key] = null;
  } else if (same(v, stored)) {
    // already stored so
  } else if (same(v, spec.default)) {
    if (stored !== null) S.draft.values[key] = null;
  } else {
    S.draft.values[key] = v;
  }
}

// After a 409: drop the parts of the draft that changed elsewhere (`conflicts`: keys, list names, slots); returns them.
export function dropConflicts(conflicts) {
  const dropped = [];
  for (const name of conflicts) {
    if (name in S.draft.values) { delete S.draft.values[name]; dropped.push(name); }
    if (name in S.draft.lists) { delete S.draft.lists[name]; dropped.push(name); }
    if (name in S.draft.secrets) { delete S.draft.secrets[name]; dropped.push(name); }
    delete S.invalid[name];
  }
  return dropped;
}

// ---------- talking to the server ----------
async function read(res) {
  return res.json().catch(() => ({}));
}

async function getJson(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(res.status === 503 ? "Settings are not ready yet. Try again in a moment." : `${url} answered ${res.status}.`);
  return res.json();
}

export async function fetchState() {
  const [schema, server] = await Promise.all([S.schema ? Promise.resolve(S.schema) : getJson("/api/settings/schema"), getJson("/api/settings")]);
  load(schema, server);
}

const post = (url, method, body, type = "application/json") =>
  fetch(url, { method, headers: { "Content-Type": type }, body: typeof body === "string" ? body : JSON.stringify(body) });

// POST /api/settings/validate -> {ok, errors, warnings, summary}; 4xx bodies carry the same shape or a SettingsError
export async function validateDraft() {
  const res = await post("/api/settings/validate", "POST", { values: S.draft.values, lists: S.draft.lists, secrets: S.draft.secrets });
  if (!res.ok && res.status !== 422 && res.status !== 400) return null;
  return read(res);
}

// PUT /api/settings -> {status, data}: 200 PutResponse; 409 ConflictError; 400/422 SettingsError
export async function putDraft(source = "") {
  const body = { base_revision: S.server.revision, values: S.draft.values, lists: S.draft.lists, secrets: S.draft.secrets };
  const res = await post(`/api/settings${source ? `?source=${source}` : ""}`, "PUT", body);
  return { status: res.status, ok: res.ok, data: await read(res) };
}

export async function fetchVersions() {
  return getJson("/api/settings/versions");
}

export async function fetchVersion(n) {
  return getJson(`/api/settings/versions/${n}`);
}

export async function fetchExport() {
  const res = await fetch("/api/settings/export", { cache: "no-store" });
  if (!res.ok) throw new Error(`The export could not be made (${res.status}).`);
  return res.text();
}

// POST /api/settings/import (the YAML text) -> ImportDraft {values, lists, notes, kept}; applies nothing
export async function importYaml(text) {
  const res = await post("/api/settings/import", "POST", text, "text/yaml");
  const data = await read(res);
  if (!res.ok) throw new Error(data.detail || `The server answered ${res.status}.`);
  return data;
}

export async function fetchEvents() {
  const data = await getJson("/api/webhooks/events");
  return data.events || {};
}
