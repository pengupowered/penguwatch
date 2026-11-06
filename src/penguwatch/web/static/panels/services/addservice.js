// Services tab: "Add a service or bundle" form (plain service or a bundle that includes other services).
import { api } from "../../js/api.js";
import { pricesChanged } from "../../js/data.js";
import { state } from "../../js/state.js";
import { esc, showToast } from "../../js/util.js";

// id = lowercase name with every run of non [a-z0-9] turned into "_", trimmed, at most 60 chars
export function slugify(name) {
  const slug = String(name || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 60);
  return slug || "service";
}

export function uniqueId(base, taken) {
  if (!taken.has(base)) return base;
  let n = 2;
  while (taken.has(`${base}-${n}`)) n += 1;
  return `${base}-${n}`;
}

// Checkbox list of the existing plain services that can be bundle members; keeps the user's ticks across re-renders.
export function renderMemberChoices(services) {
  const box = document.getElementById("add-service-members");
  if (!box) return;
  const checked = new Set([...box.querySelectorAll("input:checked")].map((i) => i.value));
  const plain = services.filter((s) => !(s.includes && s.includes.length));
  box.innerHTML = plain.length
    ? plain
        .map(
          (s) => `<label class="check member-choice"><input type="checkbox" value="${esc(s.id)}" ${checked.has(s.id) ? "checked" : ""}> ${esc(s.name)}</label>`
        )
        .join("")
    : '<span class="muted">No services yet.</span>';
}

function showError(message) {
  const el = document.getElementById("add-service-error");
  if (!el) return;
  el.textContent = message;
  el.hidden = !message;
}

function readForm() {
  const name = document.getElementById("add-service-name").value.trim();
  const priceRaw = document.getElementById("add-service-price").value.trim();
  const price = priceRaw === "" ? 0 : parseFloat(priceRaw);
  const aliases = document.getElementById("add-service-aliases").value.split(",").map((a) => a.trim()).filter(Boolean);
  const includes = [...document.querySelectorAll("#add-service-members input:checked")].map((i) => i.value);
  const isBundle = document.getElementById("add-service-is-bundle").checked;
  return { name, price, aliases, includes: isBundle ? includes : [], isBundle };
}

function validate(f) {
  if (!f.name) return "Enter a name.";
  if (isNaN(f.price) || f.price < 0) return "The price cannot be negative.";
  if (f.isBundle && f.includes.length === 0) return "Pick at least one service this bundle includes.";
  return "";
}

async function submitAddService(ev) {
  ev.preventDefault();
  const f = readForm();
  const problem = validate(f);
  showError(problem);
  if (problem) return;
  const id = uniqueId(slugify(f.name), new Set(state.services.map((s) => s.id)));
  const body = {
    id, name: f.name, monthly_cost: f.price, is_active: false, renewal_day: null, renewal_date: null,
    aliases: f.aliases, includes: f.includes, notes: null,
  };
  try {
    await api("/api/services", { method: "POST", body });
    document.getElementById("add-service-form").reset();
    toggleMembers();
    showError("");
    showToast(`${f.name} added${f.includes.length ? ` as a bundle of ${f.includes.length}` : ""}`);
    await pricesChanged();
  } catch (err) {
    showToast(err.message, true);
  }
}

function toggleMembers() {
  const on = document.getElementById("add-service-is-bundle").checked;
  const box = document.getElementById("add-service-members");
  box.hidden = !on;
}

export function setupAddService() {
  const form = document.getElementById("add-service-form");
  if (!form) return;
  form.addEventListener("submit", submitAddService);
  document.getElementById("add-service-is-bundle").addEventListener("change", toggleMembers);
  toggleMembers();
  renderMemberChoices(state.services);
}
