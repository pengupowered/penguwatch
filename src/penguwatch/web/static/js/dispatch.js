// Delegated, allow-listed event dispatch for markup that carries data-call / data-action / data-change
// attributes (the CSP forbids inline handlers). Modules register what may be called; nothing is
// reachable from the DOM unless it was registered here.
import { PLACEHOLDER_POSTER } from "./util.js";

const callable = {};   // data-call="name"          -> fn(...args)
const actions = {};    // data-action="name"        -> fn(el, ev)
const changes = {};    // data-change="name"        -> fn(el, ev)

const has = (obj, name) => Object.prototype.hasOwnProperty.call(obj, name);

export function register(handlers) {
  Object.assign(callable, handlers);
}

export function registerAction(name, fn) {
  actions[name] = fn;
}

export function registerChange(name, fn) {
  changes[name] = fn;
}

// --- generic, allow-listed dispatcher: data-call="fn" data-args='[json]' [data-on="change"] [data-prevent] ---
function dispatchCall(el, ev) {
  const name = el.dataset.call;
  if (!has(callable, name)) return;
  const fn = callable[name];
  if (typeof fn !== "function") return;
  let args;
  try { args = JSON.parse(el.dataset.args || "[]"); } catch { return; }
  args = args.map((a) => (a === "$value" ? el.value : a === "$checked" ? el.checked : a));
  if (el.dataset.prevent) ev.preventDefault();
  fn(...args);
}

export function setupDispatch() {
  document.addEventListener("click", (ev) => {
    const el = ev.target.closest("[data-call]");
    if (el && el.dataset.on !== "change") dispatchCall(el, ev);
  });
  document.addEventListener("change", (ev) => {
    const el = ev.target.closest("[data-call][data-on='change']");
    if (el) dispatchCall(el, ev);
  });
  // <img> error events do not bubble, so listen in the capture phase
  document.addEventListener("error", (ev) => {
    const img = ev.target;
    if (!img || img.tagName !== "IMG" || !img.dataset.fallback) return;
    const mode = img.dataset.fallback;
    img.removeAttribute("data-fallback");  // never loop
    if (mode === "poster") img.src = PLACEHOLDER_POSTER;
    else if (mode === "swap") { img.hidden = true; if (img.nextElementSibling) img.nextElementSibling.hidden = false; }
    else img.hidden = true;
  }, true);

  document.addEventListener("change", (ev) => {
    const el = ev.target.closest("[data-change]");
    if (!el) return;
    const name = el.dataset.change;
    if (has(changes, name)) changes[name](el, ev);
  });

  // Buttons whose data come from titles use data-* + this handler, never inline JS strings
  document.addEventListener("click", (ev) => {
    const el = ev.target.closest("[data-action]");
    if (!el) return;
    const name = el.dataset.action;
    if (has(actions, name)) actions[name](el, ev);
  });
}
