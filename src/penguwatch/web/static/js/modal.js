// Dialogs: a <div class="modal"> opened with .active (components: dialog look on desktop, bottom sheet on phones).
// Focus moves into a dialog when it opens (its [data-first-focus] element, else its close button) and back to what
// had it when it closes; Tab stays inside it, Escape and a click on the backdrop close it. One keyboard listener on
// the document serves every dialog and acts on the topmost (the one opened last: the progress dialog over the
// details). Markup closes a dialog with data-action="close-modal" on any element inside it.
import { registerAction } from "./dispatch.js";

const openers = new Map();  // modal id -> the element that had focus when it opened
const closers = new Map();  // modal id -> functions to run when it closes
const stack = [];           // the open dialogs, the topmost last

export const isOpen = (id) => Boolean(document.getElementById(id)?.classList.contains("active"));

export function onModalClose(id, fn) {
  if (!closers.has(id)) closers.set(id, []);
  closers.get(id).push(fn);
}

export function openModal(id) {
  const modal = document.getElementById(id);
  if (!modal) return;
  if (!modal.classList.contains("active")) {
    openers.set(id, document.activeElement);
    stack.push(modal);
  }
  modal.classList.add("active");
  document.body.classList.add("modal-open");
  if (!modal.contains(document.activeElement)) (modal.querySelector("[data-first-focus]") || modal.querySelector(".m-close"))?.focus();
}

export function closeModal(id) {
  const modal = document.getElementById(id);
  if (!modal || !modal.classList.contains("active")) return;
  modal.classList.remove("active");
  const at = stack.indexOf(modal);
  if (at >= 0) stack.splice(at, 1);
  if (!stack.length) document.body.classList.remove("modal-open");
  const back = openers.get(id);
  openers.delete(id);
  const lost = !document.activeElement || document.activeElement === document.body || modal.contains(document.activeElement);
  if (lost && back && back.isConnected && typeof back.focus === "function") back.focus();
  (closers.get(id) || []).forEach((fn) => fn());
}

export const topModal = () => stack[stack.length - 1] || null;

function focusable(modal) {
  return [...modal.querySelectorAll("a[href], button, input, select, textarea, summary, [tabindex]")]
    .filter((e) => !e.disabled && !e.hidden && e.tabIndex >= 0 && e.offsetParent !== null);
}

export function setupModals() {
  document.addEventListener("keydown", (ev) => {
    const modal = topModal();
    if (!modal || ev.defaultPrevented) return;
    if (ev.key === "Escape") {
      ev.preventDefault();
      closeModal(modal.id);
      return;
    }
    if (ev.key !== "Tab") return;
    const items = focusable(modal);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (!modal.contains(document.activeElement)) { ev.preventDefault(); (ev.shiftKey ? last : first).focus(); }
    else if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  });
  // a click on the backdrop (the .modal itself, around its content) closes it
  document.addEventListener("click", (ev) => {
    if (ev.target.classList?.contains("modal") && ev.target.classList.contains("active")) closeModal(ev.target.id);
  });
}

registerAction("close-modal", (el) => {
  const modal = el.closest(".modal");
  if (modal) closeModal(modal.id);
});
