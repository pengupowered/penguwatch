// System tab: sync health, API latency, upstream calls, database and security posture. Reads /api/system.
import { api } from "../../js/api.js";
import { registerAction } from "../../js/dispatch.js";
import { state } from "../../js/state.js";
import { onTabActivated } from "../../js/tabs.js";
import { esc, icon, setWidths, showToast } from "../../js/util.js";

const TAB = "system-tab";

const fmtBytes = (n) => {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / Math.pow(1024, i)).toFixed(i ? 1 : 0)} ${u[i]}`;
};
const fmtDur = (s) => {
  if (s == null) return "—";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 172800) return `${(s / 3600).toFixed(1)}h`;
  return `${Math.round(s / 86400)}d`;
};
const ago = (epochOrIso) => {
  if (!epochOrIso) return "never";
  const t = typeof epochOrIso === "number" ? epochOrIso * 1000 : Date.parse(epochOrIso);
  return Number.isNaN(t) ? "—" : `${fmtDur((Date.now() - t) / 1000)} ago`;
};

// status = icon + word + colour, never colour alone
const CHIP = { ok: ["ok", "st-good"], warn: ["warn", "st-warn"], bad: ["down", "st-crit"], idle: ["unknown", "st-unknown"] };
const chip = (kind, text) => `<span class="badge quiet ${CHIP[kind][1]}">${icon(CHIP[kind][0])}${esc(text)}</span>`;
const tile = (label, value, sub) =>
  `<div class="panel tile"><div class="stat"><span class="lbl">${esc(label)}</span><span class="val">${value}</span>${sub ? `<span class="sub">${sub}</span>` : ""}</div></div>`;
const card = (title, sub, body, ic) => `
  <div class="panel sys-card">
    <div class="panel-h">${icon(ic)}<h2 class="panel-t">${esc(title)}</h2><span class="panel-m">${sub}</span></div>
    ${body}
  </div>`;

function syncChip(s) {
  if (s.remote === false) return chip("ok", "Local only");
  if (!s.authenticated) return chip("idle", "Not connected");
  if (s.last_error) return chip("bad", "Last sync failed");
  if (s.age_seconds < 0) return chip("warn", "Never synced");
  const limit = (s.interval_minutes || 30) * 60 * 3;
  return s.age_seconds > limit ? chip("warn", `Stale (${fmtDur(s.age_seconds)})`) : chip("ok", `Synced ${fmtDur(s.age_seconds)} ago`);
}

// Event webhooks (Settings > Notifications): state, counts and the last error; never the address or the secret (the API has neither)
const HOOK_STATE = {
  ok: { kind: "ok", word: "Delivering" }, idle: { kind: "idle", word: "Nothing sent yet" }, failing: { kind: "bad", word: "Failing" },
  dropping: { kind: "warn", word: "Dropping events" }, disabled: { kind: "bad", word: "Off" },
};

function webhooksCard(w) {
  const hooks = (w && w.hooks) || [];
  if (!hooks.length) {
    return card("Event webhooks", "calls to your own URLs", `<div class="panel-b"><p class="muted fine">None configured. Add one in Settings (Notifications) to be called when a title is added, a status changes or an episode is watched.</p><button type="button" class="btn sm outline" data-action="open-settings-tab">Open Settings</button></div>`, "link");
  }
  const rows = hooks.map((h) => {
    const { kind, word } = HOOK_STATE[h.state] || HOOK_STATE.idle;
    return `<tr>
      <th scope="row">${esc(h.name)}<div class="muted fine">${esc(h.host)}${h.signed ? "" : " (unsigned)"}</div></th>
      <td>${chip(kind, word)}</td>
      <td class="num">${h.delivered}</td><td class="num">${h.failed}</td><td class="num">${h.dropped}</td><td class="num">${h.queued}</td>
      <td>${esc(h.last_success_at ? ago(h.last_success_at) : "never")}</td>
      <td class="sys-err" title="${esc(h.disabled_reason || h.last_error || "")}">${esc(h.disabled_reason || h.last_error || "")}</td>
      <td>${h.state === "disabled" ? "" : `<button type="button" class="btn sm outline" data-action="webhook-test" data-name="${esc(h.name)}">Test</button>`}</td>
    </tr>`;
  }).join("");
  const body = `<div class="panel-b flush"><div class="table-wrap"><table class="tbl"><thead><tr><th>Webhook</th><th>State</th><th class="num">Delivered</th><th class="num">Failed</th><th class="num">Dropped</th><th class="num">Queued</th><th>Last delivered</th><th>Last error</th><th></th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
  return card("Event webhooks", "the address and secret are never shown", body, "link");
}

function render(d) {
  const s = d.sync, ob = s.outbox || {};
  const pending = (ob.total || 0) - (ob.awaiting || 0);
  const upstream = d.upstream || {};
  const upErr = Object.values(upstream).reduce((n, u) => n + (u.errors || 0), 0);
  const maxP95 = Math.max(1, ...d.http.routes.map((r) => r.p95_ms));

  const secWarn = d.security && !d.security.auth_required
    ? `<div class="result st-warn">${icon("warn")}<span><b>No access token set.</b> Anyone who can reach this port can read and change the library and its account link. Set <code>PENGUWATCH_ACCESS_TOKEN</code> if it's reachable beyond this PC.</span></div>`
    : "";

  const setupWarn = (d.warnings || []).map((w) => `<div class="result st-warn">${icon("warn")}<span>${esc(w)}</span></div>`).join("");

  const history = (s.history || []).slice(0, 10).map((h) => {
    const ok = h.status === "success";
    return `<tr>
      <td>${esc(ago(h.started_at))}</td>
      <td>${esc(h.trigger || "")}</td>
      <td>${chip(ok ? "ok" : h.status === "skipped" ? "idle" : "bad", h.status || "?")}</td>
      <td>${esc(h.mode || "—")}</td>
      <td class="num">${h.pushed ?? "—"}</td>
      <td class="num">${h.updated ?? "—"}</td>
      <td class="num">${h.duration_s != null ? h.duration_s + "s" : "—"}</td>
      <td class="sys-err">${esc(h.error || "")}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="8" class="muted">No sync has run since the container started.</td></tr>`;

  const routes = d.http.routes.map((r) => `<tr>
      <th scope="row"><code>${esc(r.route)}</code></th>
      <td class="num">${r.count}</td>
      <td class="num">${r.p50_ms} ms</td>
      <td><div class="bar"><div class="bar-track"><div class="bar-fill" data-w="${Math.max(3, Math.round((r.p95_ms / maxP95) * 100))}"></div></div><span>${r.p95_ms} ms</span></div></td>
      <td class="num">${r.max_ms} ms</td>
      <td class="num">${r.errors ? chip("bad", String(r.errors)) : "0"}</td>
    </tr>`).join("") || `<tr><td colspan="6" class="muted">No requests yet.</td></tr>`;

  const ups = Object.entries(upstream).map(([name, u]) => `<tr>
      <th scope="row">${esc(name.toUpperCase())}</th>
      <td class="num">${u.requests}</td>
      <td class="num">${u.errors ? chip("warn", String(u.errors)) : "0"}</td>
      <td class="num">${u.seconds}s</td>
      <td>${u.last ? `${esc(u.last.endpoint)}: ${u.last.status || "network error"} in ${u.last.ms} ms` : "—"}</td>
    </tr>`).join("") || `<tr><td colspan="5" class="muted">No upstream calls yet.</td></tr>`;

  const tbl = (head, body) => `<div class="panel-b flush"><div class="table-wrap"><table class="tbl"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div></div>`;
  const root = document.getElementById("system-container");
  root.innerHTML = `
    ${secWarn}
    ${setupWarn}
    <div class="panel sys-status-row">
      <div class="sys-status"><span class="sys-status-lbl">${esc(s.label || "Library")} sync</span>${syncChip(s)}</div>
      <div class="sys-status"><span class="sys-status-lbl">Outbox</span>${(ob.failing || 0) > 0 ? chip("bad", `${ob.failing} failing`) : pending > 0 ? chip("warn", `${pending} pending`) : chip("ok", "empty")}${ob.awaiting ? ` <span class="muted">· ${ob.awaiting} awaiting your approval</span>` : ""}</div>
      <div class="sys-status"><span class="sys-status-lbl">Server errors</span>${d.http.server_errors ? chip("bad", `${d.http.server_errors} (5xx)`) : chip("ok", "none")}</div>
      <div class="sys-status"><span class="sys-status-lbl">Upstream errors</span>${upErr ? chip("warn", String(upErr)) : chip("ok", "none")}</div>
      <div class="sys-status"><span class="sys-status-lbl">Logged</span>${d.logs.error ? chip("bad", `${d.logs.error} errors`) : d.logs.warning ? chip("warn", `${d.logs.warning} warnings`) : chip("ok", "clean")}</div>
    </div>

    <div class="tiles">
      ${tile("Version", esc(d.version), `Python ${esc(d.python)}`)}
      ${tile("Uptime", fmtDur(d.uptime_seconds), `since ${new Date(d.started_at * 1000).toLocaleString()}`)}
      ${tile("Memory", fmtBytes(d.rss_bytes))}
      ${tile("Database", fmtBytes(d.database.bytes), `WAL ${fmtBytes(d.database.wal_bytes)}`)}
      ${tile("Titles", d.database.shows.toLocaleString(), `${d.database.episodes.toLocaleString()} episodes`)}
      ${tile("Requests", d.http.requests.toLocaleString(), `${d.http.client_errors} client errors · ${d.http.in_flight} in flight`)}
    </div>

    ${card("Sync history", `last runs since the container started (every ${s.interval_minutes || 30} minutes, plus manual and login runs)`,
      tbl(`<th>Started</th><th>Trigger</th><th>Result</th><th>Mode</th><th class="num">Pushed</th><th class="num">Updated</th><th class="num">Took</th><th>Error</th>`, history), "history")}

    ${card("API latency by route", "slowest first, by 95th percentile over the last 300 requests per route",
      tbl(`<th>Route</th><th class="num">Requests</th><th class="num">p50</th><th>p95</th><th class="num">Max</th><th class="num">5xx</th>`, routes), "gauge")}

    ${card("Upstream services", "calls this app made to other services",
      tbl(`<th>Service</th><th class="num">Requests</th><th class="num">Errors</th><th class="num">Waited</th><th>Last call</th>`, ups), "globe")}

    ${webhooksCard(d.webhooks)}

    ${card("Endpoints for your tools", "point Prometheus, Uptime Kuma or a script at these", `<div class="panel-b">
      <ul class="sys-links">
        <li><a href="/metrics" target="_blank" rel="noopener"><code>/metrics</code></a> Prometheus text format</li>
        <li><a href="/api/health/ready" target="_blank" rel="noopener"><code>/api/health/ready</code></a> readiness (database reachable)</li>
        <li><a href="/api/system" target="_blank" rel="noopener"><code>/api/system</code></a> everything on this page as JSON</li>
        <li><a href="/openapi.json" target="_blank" rel="noopener"><code>/openapi.json</code></a> the API reference (import it into any OpenAPI viewer)</li>
      </ul>
      <p class="muted fine">Every response carries <code>X-Request-ID</code> and <code>Server-Timing</code>; the same id appears in the container log lines for that request.</p>
    </div>`, "link")}`;
  setWidths(root);
}

async function loadSystem() {
  const root = document.getElementById("system-container");
  if (!root) return;
  try {
    render(await api("/api/system"));
  } catch (e) {
    root.innerHTML = `<div class="empty st st-crit">${icon("warn")}<span><b>Couldn't load system status</b>${esc(e.message)}</span></div>`;
  }
}

async function testWebhook(el) {
  el.disabled = true;
  try {
    const r = await api(`/api/webhooks/${encodeURIComponent(el.dataset.name)}/test`, { method: "POST" });
    showToast(r.delivered ? "Test event delivered" : `Test event failed: ${r.error || "no answer"}`, !r.delivered);
  } catch (e) {
    showToast(e.message, true);
  }
  loadSystem();
}

registerAction("webhook-test", testWebhook);
registerAction("open-settings-tab", () => { location.hash = "#/settings"; });
onTabActivated(TAB, loadSystem);

export default {
  id: "system",
  mount() {
    setInterval(() => {
      if (state.activeTab === TAB && document.visibilityState === "visible") loadSystem();
    }, 10000);
  },
};
