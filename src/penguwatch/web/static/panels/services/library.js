// The library driver's card: its status, sync, disconnect, credentials form and connect flow (pin.html draws the
// "code" step). Nothing here names a service: the label, the account's controls and the paths come from the drivers
// (js/drivers.js, /api/drivers). The status itself is fetched by js/library-status.js, which the header shows on every
// tab; this card draws the details from it ("library-status" on the bus).
import { api } from "../../js/api.js";
import { on } from "../../js/bus.js";
import { loadAllData } from "../../js/data.js";
import { libraryLabel } from "../../js/drivers.js";
import { register } from "../../js/dispatch.js";
import { closeModal, onModalClose, openModal } from "../../js/modal.js";
import { checkLibraryStatus } from "../../js/library-status.js";
import { state } from "../../js/state.js";
import { goToTab, onTabActivated } from "../../js/tabs.js";
import { esc, icon, showToast } from "../../js/util.js";
import { openReview, renderReview } from "./review.js";

const TAB = "services-tab";
let connectPoll = null;

const fmtDate = (iso) => (iso ? new Date(iso).toLocaleDateString() : "");
const show = (id, on) => { document.getElementById(id).hidden = !on; };
const driverPath = (action) => `/api/drivers/${encodeURIComponent(state.drivers.library)}/${action}`;

export function setupLibraryListeners() {
  document.getElementById("btn-toggle-manual-token").addEventListener("click", (ev) => {
    const form = document.getElementById("library-manual-section");
    form.hidden = !form.hidden;
    ev.currentTarget.setAttribute("aria-expanded", String(!form.hidden));
  });
  document.getElementById("btn-save-credentials").addEventListener("click", async () => {
    try {
      await api(driverPath("credentials"), {
        method: "POST",
        body: {
          client_id: document.getElementById("input-client-id").value.trim(),
          access_token: document.getElementById("input-access-token").value.trim(),
        },
      });
      showToast(`${libraryLabel()} credentials saved`);
      checkLibraryStatus();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  const runSync = async (full) => {
    showToast(full ? `Re-reading your whole ${libraryLabel()} library…` : `Syncing with ${libraryLabel()}…`);
    try {
      const data = await api(`${driverPath("sync")}${full ? "?full=true" : ""}`, { method: "POST" });
      const parts = [];
      if (data.pushed) parts.push(`sent ${data.pushed}`);
      parts.push(data.updated ? `updated ${data.updated}` : `no changes on ${libraryLabel()}`);
      if (data.removed) parts.push(`removed ${data.removed}`);
      if (data.staged) parts.push(`${data.staged} change(s) from ${libraryLabel()} to review`);
      if (data.awaiting_approval) parts.push(`${data.awaiting_approval} of yours awaiting approval`);
      await loadAllData();
      if (data.staged || data.awaiting_approval) {
        openReview();
      } else {
        showToast(`Sync done: ${parts.join(", ")}`);
      }
    } catch (err) {
      showToast(err.message, true);
      checkLibraryStatus();
    }
  };
  document.getElementById("btn-trigger-sync").addEventListener("click", () => runSync(false));
  document.getElementById("btn-full-sync").addEventListener("click", () => runSync(true));
  document.getElementById("btn-retry-pushes").addEventListener("click", async () => {
    try {
      const r = await api("/api/sync/outbox/retry", { method: "POST" });
      showToast(`Sent ${r.sent}, ${r.remaining} still waiting`);
      checkLibraryStatus();
    } catch (err) {
      showToast(err.message, true);
    }
  });
  document.getElementById("btn-library-disconnect").addEventListener("click", async () => {
    if (!confirm(`Disconnect ${libraryLabel()}? Your library stays here; syncing stops until you reconnect.`)) return;
    await api(driverPath("disconnect"), { method: "POST" });
    checkLibraryStatus();
  });

  document.getElementById("btn-start-connect").addEventListener("click", startConnectFlow);
  onModalClose("pin-modal", stopPolling);
}

function setBadge(cls, ic, word, text) {
  const badge = document.getElementById("library-box-badge");
  badge.className = `badge ${cls}`;
  badge.querySelector("use").setAttribute("href", `#i-${ic}`);
  document.getElementById("library-box-word").textContent = word;
  document.getElementById("library-box-text").textContent = text;
}

// The card's details. A light check (the poll, after a change) only redraws them while this tab is open.
function renderLibrary({ data, light }) {
  if (light && state.activeTab !== TAB) return;
  const name = data.label || libraryLabel();
  // No account behind the library: say so, and leave the account's controls (hidden by the page) alone
  if (data.remote === false) {
    setBadge("st-good", "ok", "Local only", data.auth_message || "Nothing is sent to an account.");
    show("library-account-details", false);
    show("library-renewal-note", false);
    show("library-error", false);
    renderWaitingNote(data, name);
    return;
  }
  renderWaitingNote(data, name);
  // Renewal was refused but the login still works: a note here, not the app-wide banner
  const renewalNote = document.getElementById("library-renewal-note");
  renewalNote.hidden = !data.renewal_note;
  renewalNote.innerHTML = data.renewal_note ? `${icon("info")}<span>${esc(data.renewal_note)}</span>` : "";

  const lastError = data.can_write === false && !data.last_error
    ? `Your ${name} login is read-only. Click 'Connect ${name}' and approve again to allow sending changes.`
    : data.last_error;
  const errBox = document.getElementById("library-error");
  errBox.hidden = !lastError;
  errBox.innerHTML = lastError ? `${icon("warn")}<span>${esc(lastError)}</span>` : "";

  if (data.authenticated) {
    if (data.last_error) setBadge("st-warn", "warn", "Problem", `${name} connected, but the last sync had a problem`);
    else setBadge("st-good", "ok", "Connected", "Two-way sync on");
    show("library-account-details", true);
    document.getElementById("library-username").textContent = (data.user && data.user.name) || "—";
    document.getElementById("library-vip-status").textContent = (data.account && (data.account.type || data.account.plan)) || "Connected";
    document.getElementById("library-last-sync-time").textContent = data.last_sync ? new Date(data.last_sync).toLocaleString() : "Not yet";
    const parts = [];
    if (data.awaiting_approval) parts.push(`${data.awaiting_approval} awaiting your approval`);
    if (data.pending_pushes) parts.push(`${data.pending_pushes} approved, sending${data.failed_pushes ? ` (${data.failed_pushes} retrying)` : ""}`);
    if (data.incoming_to_review) parts.push(`${data.incoming_to_review} from ${name} to review`);
    let renewal;
    if (data.auth_state === "needs_reconnect") {
      renewal = "Needs reconnect";
    } else if (data.renewal_note) {
      renewal = "Automatic renewal failed" + (data.token_expires_at ? ` · expires ${fmtDate(data.token_expires_at)}` : "");
    } else if (data.can_auto_renew) {
      renewal = "Renews automatically" + (data.token_renewed_at ? ` · last renewed ${fmtDate(data.token_renewed_at)}` : "");
    } else {
      renewal = `This ${name} login can't renew automatically` + (data.token_expires_at ? ` · expires ${fmtDate(data.token_expires_at)}` : "");
    }
    document.getElementById("library-token-renewal").textContent = renewal;
    document.getElementById("library-pending-count").textContent = parts.length ? parts.join(" · ") : "Nothing, all in sync";
    show("btn-library-disconnect", true);
  } else {
    setBadge("st-unknown", "plug", "Not connected", data.client_id_configured ? "Client ID set. Authorization required." : `${name} not connected.`);
    show("library-account-details", false);
    show("btn-library-disconnect", false);
  }
  show("btn-retry-pushes", Boolean(data.failed_pushes));
  document.getElementById("review-mode-toggle").checked = data.review_mode !== false;
  renderReview();
}

// Changes queued for a driver that is not the library driver now: kept, sent when it is the library driver again
function renderWaitingNote(data, name) {
  const box = document.getElementById("library-waiting-note");
  const waiting = data.waiting_for_inactive || 0;
  box.hidden = !waiting;
  box.innerHTML = waiting
    ? `${icon("clock")}<span>${waiting} change${waiting === 1 ? " is" : "s are"} waiting for a driver that is not the library driver now. They are kept, and go out when it is again.</span>`
    : "";
}

// The driver's connect flow, drawn from its steps (ConnectStep): "code" (a code to enter at a URL; the dialog polls
// with the step's state), "pending" (not approved yet, poll again), "done" and "error" (the API answers 400).
async function startConnectFlow() {
  try {
    stopPolling();
    handleStep(await api(driverPath("connect"), { method: "POST" }), null);
  } catch (err) {
    showToast(err.message, true);
  }
}

function stopPolling() {
  if (connectPoll) clearInterval(connectPoll);
  connectPoll = null;
}

function handleStep(step, previous) {
  if (step.kind === "done") {
    stopPolling();
    closeModal("pin-modal");
    showToast(`${libraryLabel()} connected! Importing your library in the background…`);
    checkLibraryStatus();
    setTimeout(loadAllData, 15000);
    return;
  }
  if (step.kind === "pending") return;
  if (step.kind !== "code") {
    showToast(step.message || "Could not connect", true);
    return;
  }
  document.getElementById("pin-display-code").textContent = step.code || "";
  const link = document.getElementById("pin-verification-link");
  const prefilled = Boolean(step.url && step.code && step.url.includes(step.code));
  link.hidden = !step.url;
  if (step.url) {
    link.href = step.url;
    link.textContent = prefilled ? `Open ${libraryLabel()} Approval Page (Auto-Filled)` : step.url.replace(/^https?:\/\//, "");
  }
  openModal("pin-modal");
  stopPolling();
  connectPoll = setInterval(async () => {
    try {
      handleStep(await api(driverPath("connect"), { method: "POST", body: { state: step.state } }), step);
    } catch (e) {
      console.error("Polling error:", e);
    }
  }, (step.interval || 5) * 1000);
}

onTabActivated(TAB, () => checkLibraryStatus());
on("library-status", renderLibrary);
// The header's status pill opens this tab; the reconnect banner's button starts the connect flow (markup data-call)
register({ startReconnect: () => startConnectFlow(), openLibrary: () => goToTab(TAB) });
