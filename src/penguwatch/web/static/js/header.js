// The header: the library driver's status pill (icon, word and colour, with the age of the last sync), the "to review" and
// "sending" chips, the spending figures, and the app-wide reconnect banner under the header. Everything comes from
// data other modules already fetched (the bus: "library-status", "drivers-loaded", "stats", "plan"); no requests of its own.
import { on } from "./bus.js";
import { libraryLabel } from "./drivers.js";
import { state } from "./state.js";

const STATES = {
  checking: ["st-unknown", "unknown", "Checking"],
  disconnected: ["st-unknown", "plug", "Not connected"],
  expired: ["st-crit", "down", "Login expired"],
  problem: ["st-warn", "warn", "Sync problem"],
  expiring: ["st-warn", "warn", "Login expiring"],
  readonly: ["st-warn", "warn", "Read-only"],
  never: ["st-unknown", "unknown", "Not synced yet"],
  local: ["st-good", "ok", "Local only"],
  current: ["st-good", "ok", "Synced"],
};

function syncAge(iso) {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  if (mins < 48 * 60) return `${Math.round(mins / 60)} h ago`;
  return `${Math.round(mins / 1440)} d ago`;
}

function stateOf(st) {
  if (!st) return "checking";
  if (st.remote === false) return "local";  // no account behind the library: nothing to connect or sync
  if (!st.authenticated) return "disconnected";
  if (st.auth_state === "needs_reconnect") return "expired";
  if (st.last_error) return "problem";
  if (st.auth_state === "expiring") return "expiring";
  if (st.can_write === false) return "readonly";
  return st.last_sync ? "current" : "never";
}

// Uses what checkLibraryStatus() already fetched (state.libraryStatus); no extra request
function renderHeaderLine() {
  const st = state.libraryStatus;
  const pill = document.getElementById("btn-open-library");
  const word = document.getElementById("library-badge-text");
  const sync = document.getElementById("header-sync");
  const review = document.getElementById("header-review");
  const sending = document.getElementById("library-pending-badge");
  if (!pill || !word || !sync || !st) return;
  const [cls, ic, label] = STATES[stateOf(st)];
  const name = st.label || libraryLabel();
  const source = pill.querySelector(".sp-src");
  if (source) {
    source.textContent = name;
    source.hidden = st.remote === false;  // "Local only" already says it
  }
  pill.className = `status-pill ${cls}`;
  pill.querySelector(".sp-ic use")?.setAttribute("href", `#i-${ic}`);
  word.textContent = label;

  const age = st.last_sync ? syncAge(st.last_sync) : "";
  const local = st.remote === false;
  let text = age || (!st.authenticated ? `${name} not connected` : "not synced yet");
  if (local) text = "Local only: nothing is sent to an account";
  if (st.auth_state === "needs_reconnect") text = `${name} login expired`;
  const fresh = age && st.auth_state !== "needs_reconnect" && !local;
  sync.textContent = fresh ? age : "";
  sync.hidden = !fresh;
  const full = fresh ? `Last ${name} sync ${age}` : text;
  pill.title = `${full}. Open Services & Sync`;
  pill.setAttribute("aria-label", `${name}: ${label}. ${full}. Open Services & Sync`);

  const n = st.remote === false ? 0 : (st.awaiting_approval || 0) + (st.incoming_to_review || 0);
  if (review) {
    review.hidden = !n;
    review.querySelector("span").textContent = n ? `${n} to review` : "";
    review.title = "Changes waiting for your approval (Services & Sync, Review Changes)";
  }
  if (sending) {
    sending.hidden = !st.pending_pushes;
    sending.querySelector("span").textContent = st.pending_pushes ? `${st.pending_pushes} sending` : "";
    sending.title = `Approved changes being sent to ${name}`;
  }
}

const fmtDate = (iso) => (iso ? new Date(iso).toLocaleDateString() : "");

// App-wide: visible on every tab (the 60 s status poll keeps it current) until the login works again.
function renderAuthBanner(data) {
  const banner = document.getElementById("auth-banner");
  const text = document.getElementById("auth-banner-text");
  if (!banner || !text) return;
  const needs = data.auth_state === "needs_reconnect";
  const expiring = data.auth_state === "expiring";
  banner.hidden = !(needs || expiring);
  banner.className = `auth-banner ${needs ? "st-crit" : "st-warn"}`;
  if (needs) {
    text.textContent = data.auth_message || `Your ${data.label || libraryLabel()} login needs renewing. Sync is paused until you reconnect.`;
  } else if (expiring) {
    const when = fmtDate(data.token_expires_at);
    text.textContent = `Your ${data.label || libraryLabel()} login expires${when ? ` ${when}` : " soon"} and can't renew automatically.`;
  } else {
    text.textContent = "";
  }
}

const money = (n) => `$${Number(n || 0).toFixed(2)}`;

export function setupHeader() {
  on("library-status", ({ data }) => { renderAuthBanner(data); renderHeaderLine(); });
  on("drivers-loaded", () => renderHeaderLine());
  on("stats", (stats) => { document.getElementById("head-spend").textContent = money(stats.current_monthly_spend); });
  // What cancelling would really save; the raw "no viewing" figure only when the plan has no status data.
  on("plan", (plan) => {
    const unused = plan.savable_monthly_cost !== undefined ? plan.savable_monthly_cost : plan.unused_monthly_cost;
    document.getElementById("head-unused").textContent = money(unused);
  });
  renderHeaderLine();
  setInterval(renderHeaderLine, 5000);
}
