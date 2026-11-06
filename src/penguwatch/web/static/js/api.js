// Thin fetch wrapper: JSON bodies, JSON/text responses, errors carry the server "detail".
// It also times every request: a "Loading…" pill appears while any request is slow, and a request that took
// SLOW_MS or more in the browser is reported to /api/client-timing with where the time went, so the server
// log can tell a slow server from a slow network or a waking device.
import { esc, icon } from "./util.js";

const SLOW_MS = 2000;
const PILL_AFTER_MS = 400;
const REPORT_PATH = "/api/client-timing";

const pending = new Map();  // request id -> start time (performance.now())
let nextId = 0;
let pillTimer = null;
let lastVisibleAt = document.visibilityState === "visible" ? performance.now() : null;

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") lastVisibleAt = performance.now();
});
// The default buffer (250 entries) fills within hours on a page left open; keep room for new requests.
performance.addEventListener?.("resourcetimingbufferfull", () => performance.clearResourceTimings());

// A sign-in that ended while the page is open (signed out elsewhere, expired): homeguard refuses with 401 and
// `WWW-Authenticate: Bearer realm=...`. The page then goes to the sign-in page, once. Without a token nothing is
// refused; other 401s (none today) would not carry that header.
let leaving = false;
export function signInRefused(res) {
  return res.status === 401 && /^Bearer realm=/i.test(res.headers.get("www-authenticate") || "");
}
function sessionEnded() {
  if (leaving) return;
  leaving = true;
  window.location.assign("/login");
}

// The server's "detail" is a sentence, or (a 422) a list of {loc, msg}: one readable line either way.
export function detailText(detail) {
  if (Array.isArray(detail)) return detail.map((d) => (d && d.msg ? d.msg : String(d))).join("; ");
  return typeof detail === "string" ? detail : "";
}

export async function api(path, options = {}) {
  const opts = { ...options };
  if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  }
  const id = ++nextId;
  const started = performance.now();
  const wasHidden = document.visibilityState !== "visible";
  pending.set(id, started);
  schedulePill();
  let res = null;
  try {
    res = await fetch(path, opts);
    if (signInRefused(res)) sessionEnded();
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(detailText(err.detail) || `Request failed (${res.status})`);
    }
    const type = res.headers.get("content-type") || "";
    return await (type.includes("application/json") ? res.json() : res.text());
  } finally {
    pending.delete(id);
    schedulePill();
    const total = performance.now() - started;
    if (total >= SLOW_MS) reportSlow(path, (opts.method || "GET").toUpperCase(), res, started, total, wasHidden);
  }
}

// --- slow-request reports ------------------------------------------------------------------------------

function serverMs(res) {
  const m = /app;dur=([\d.]+)/.exec(res?.headers.get("server-timing") || "");
  return m ? Number(m[1]) : null;
}

// The browser's own phase timings for this request (same-origin, so they are all exposed)
function phases(path, started) {
  const url = new URL(path, location.href).href;
  const entry = performance.getEntriesByName(url, "resource").filter((e) => e.startTime >= started - 1).pop();
  if (!entry || !entry.requestStart) return {};
  const ms = (v) => Math.max(0, Math.round(v));
  return {
    page_ms: ms(entry.startTime - started),  // before the browser even began the request: a busy or frozen page
    wait_ms: ms(entry.requestStart - entry.startTime),
    connect_ms: entry.connectEnd > entry.connectStart ? ms(entry.connectEnd - entry.connectStart) : null,
    ttfb_ms: ms(entry.responseStart - entry.requestStart),
    download_ms: ms(entry.responseEnd - entry.responseStart),
  };
}

function reportSlow(path, method, res, started, total, wasHidden) {
  const pathname = new URL(path, location.href).pathname;
  if (pathname === REPORT_PATH || !pathname.startsWith("/api/")) return;
  const now = performance.now();
  const report = {
    method, path: pathname, status: res ? res.status : 0, total_ms: Math.round(total),
    server_ms: res ? serverMs(res) : null, ...phases(path, started),
    visible: document.visibilityState === "visible" && !wasHidden, online: navigator.onLine,
    page_age_s: Math.round(now / 1000),
    since_visible_s: lastVisibleAt === null ? null : Math.round((now - lastVisibleAt) / 1000),
    connection: navigator.connection?.effectiveType || null,
  };
  fetch(REPORT_PATH, {
    method: "POST", keepalive: true, headers: { "Content-Type": "application/json" }, body: JSON.stringify(report),
  }).catch(() => {});
}

// --- "Loading…" pill -----------------------------------------------------------------------------------

function pillEl() {
  let el = document.getElementById("net-status");
  if (!el) {
    el = document.createElement("div");
    el.id = "net-status";
    el.className = "net-status";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    document.body.appendChild(el);
  }
  return el;
}

function schedulePill() {
  if (pillTimer) return;
  pillTimer = setTimeout(updatePill, 200);
}

function updatePill() {
  pillTimer = null;
  const now = performance.now();
  const oldest = pending.size ? Math.min(...pending.values()) : null;
  const waited = oldest === null ? 0 : now - oldest;
  const el = document.getElementById("net-status");
  if (waited < PILL_AFTER_MS) {
    if (el) el.classList.remove("visible", "slow");
  } else {
    const pill = pillEl();
    const slow = waited >= SLOW_MS;
    pill.textContent = slow ? `Still waiting for the server… ${Math.round(waited / 1000)} s` : "Loading…";
    pill.classList.add("visible");
    pill.classList.toggle("slow", slow);
  }
  if (pending.size) pillTimer = setTimeout(updatePill, 250);
}

// A placeholder for a list that has nothing to show yet; a list that already has content keeps it while
// it refreshes (the pill says that something is loading).
export function showLoading(container, text = "Loading…") {
  if (container && !container.children.length) {
    container.innerHTML = `<div class="empty loading-state"><span class="spinner" aria-hidden="true"></span><span>${text}</span></div>`;
  }
}

// A load that failed while its placeholder was still up: say so instead of "Loading…" forever.
export function showLoadError(container, err) {
  const placeholder = container?.querySelector(":scope > .loading-state");
  if (placeholder) {
    placeholder.className = "empty st st-crit";
    placeholder.innerHTML = `${icon("warn")}<span><b>Couldn't load this</b>${esc(err?.message || err)}. It will retry on the next refresh.</span>`;
  }
}
