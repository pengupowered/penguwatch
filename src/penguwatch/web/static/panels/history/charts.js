// History tab: SVG column chart and bar list. The SVG carries classes only; panel.css colours them from the tokens.
import { esc } from "../../js/util.js";

export const fmt = (n) => (n >= 100 ? Math.round(n).toLocaleString() : (Math.round(n * 10) / 10).toString());

// The smallest "round" number (1, 2, 5 times a power of ten) that is at least `v`
function niceCeil(v) {
  if (v <= 0) return 1;
  const pow = 10 ** Math.floor(Math.log10(v));
  const f = v / pow;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * pow;
}

// items: anything; label(item) -> { short, long }; every `labelEvery`th item gets its short label; partial(item) draws
// the bar lighter (a period that is only partly inside the window). `W` is the drawing's width in CSS pixels.
export function columnChart(items, { label, value, unit, height = 170, W = 760, labelEvery = 1, partial = () => false }) {
  const padL = 34, padB = 24, padT = 12;
  const niceMax = niceCeil(Math.max(0, ...items.map(value)));
  const plotW = W - padL - 6, plotH = height - padB - padT;
  const slot = plotW / items.length, bw = Math.max(2, Math.min(34, slot * 0.66));
  const y = (v) => padT + plotH - (v / niceMax) * plotH;
  const grid = [0, 0.5, 1].map((f) => `<line x1="${padL}" x2="${W - 6}" y1="${y(niceMax * f)}" y2="${y(niceMax * f)}" class="hc-grid"/><text x="${padL - 6}" y="${y(niceMax * f) + 4}" class="hc-axis" text-anchor="end">${fmt(niceMax * f)}</text>`).join("");
  const bars = items.map((it, i) => {
    const v = value(it), x = padL + slot * i + (slot - bw) / 2, top = y(v);
    const h = Math.max(v > 0 ? 2 : 0, padT + plotH - top);
    const r = Math.min(3, bw / 2, h);
    const path = `M${x},${padT + plotH} V${padT + plotH - h + r} Q${x},${padT + plotH - h} ${x + r},${padT + plotH - h} H${x + bw - r} Q${x + bw},${padT + plotH - h} ${x + bw},${padT + plotH - h + r} V${padT + plotH} Z`;
    const lbl = label(it);
    return `<g><path d="${path}" class="hc-bar${partial(it) ? " hc-partial" : ""}" data-partial="${partial(it)}"><title>${esc(lbl.long)}: ${fmt(v)} ${unit}</title></path>
      ${i % labelEvery === 0 ? `<text x="${x + bw / 2}" y="${height - 7}" class="hc-axis" text-anchor="middle">${esc(lbl.short)}</text>` : ""}</g>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${height}" class="hc-svg" role="img" aria-label="${esc(unit)} per period">${grid}${bars}</svg>`;
}

// rows: [{ name, value, sub }]; the bar is the value against the largest
export function barList(rows, unit) {
  const max = Math.max(0.1, ...rows.map((r) => r.value));
  return `<div class="hl">${rows.map((r) => `
    <div class="hl-row" title="${esc(r.name)}: ${fmt(r.value)} ${unit}">
      <div class="hl-name">${esc(r.name)}${r.sub ? `<span class="hist-sub">${esc(r.sub)}</span>` : ""}</div>
      <div class="bar-track"><div class="bar-fill" data-w="${Math.max(2, (r.value / max) * 100)}"></div></div>
      <div class="hl-val">${fmt(r.value)} ${unit}</div>
    </div>`).join("")}</div>`;
}

export const table = (head, rows) =>
  `<details class="hc-table"><summary>Show as table</summary><div class="table-wrap"><table class="tbl"><thead><tr>${head.map((h, i) => `<th class="${i ? "num" : ""}">${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c, i) => `<td class="${i ? "num" : ""}">${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div></details>`;
