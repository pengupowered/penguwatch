// CSV for the rating import: a small RFC 4180 parser (quoted fields, doubled quotes, newlines inside quotes), the
// column mapping, and the writer for the rows that did not match. Runs in the browser; nothing here touches the page.

// text -> array of rows (arrays of strings). A byte-order mark and a final empty line are dropped.
export function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  const src = text.replace(/^﻿/, "");
  for (let i = 0; i < src.length; i++) {
    const c = src[i];
    if (quoted) {
      if (c === '"') {
        if (src[i + 1] === '"') { field += '"'; i++; } else quoted = false;
      } else field += c;
    } else if (c === '"' && field === "") quoted = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && src[i + 1] === "\n") i++;
      row.push(field); field = "";
      rows.push(row); row = [];
    } else field += c;
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  return rows.filter((r) => r.some((f) => f.trim() !== ""));
}

const RATING_COLUMNS = ["rating", "score", "value"];

// Titles are found by the ids in a title's `ids`: the drivers' own (`extra`: js/drivers.js keyProviders) and these,
// each read from its `<name>_id` column.
const ID_COLUMNS = ["imdb", "tmdb", "tvdb"];

// Header row -> {index of each known column}. Names are compared in lower case, spaces and dashes as underscores.
export function mapColumns(header, extra = []) {
  const names = header.map((h) => h.trim().toLowerCase().replace(/[\s-]+/g, "_"));
  const at = (...wanted) => names.findIndex((n) => wanted.includes(n));
  const ids = {};
  for (const provider of [...new Set([...extra, ...ID_COLUMNS])]) ids[provider] = at(`${provider}_id`, provider);
  return {
    rating: at(...RATING_COLUMNS), title: at("title", "name"), year: at("year"), media_type: at("media_type", "type"),
    ids, votes: at("votes"), url: at("url", "link"),
  };
}

const cell = (row, i) => (i >= 0 && i < row.length ? row[i].trim() : "");

// One data row -> {item} for the API, or {error} saying why the row cannot be sent.
export function rowToItem(row, cols) {
  const value = Number(cell(row, cols.rating).replace(",", "."));
  if (cell(row, cols.rating) === "" || !Number.isFinite(value)) return { error: "no usable rating" };
  const match = {};
  // the first id column with a value: the drivers' own ids first (they identify a title in the library), then IMDb, TMDB, TVDB
  const provider = Object.keys(cols.ids).find((p) => cell(row, cols.ids[p]));
  if (provider) {
    const raw = cell(row, cols.ids[provider]);
    match[provider] = provider === "imdb" || !/^\d+$/.test(raw) ? raw : Number(raw);
  } else if (cell(row, cols.title)) {
    match.title = cell(row, cols.title);
    if (cell(row, cols.year)) match.year = Number(cell(row, cols.year));
  } else return { error: "no id and no title" };
  if (cell(row, cols.media_type)) match.media_type = cell(row, cols.media_type).toLowerCase();
  const item = { match, value };
  if (cell(row, cols.votes)) item.votes = Number(cell(row, cols.votes));
  if (cell(row, cols.url)) item.url = cell(row, cols.url);
  return { item };
}

// Rows -> CSV text. A field that starts with = + - @ is prefixed with ' so a spreadsheet does not run it as a formula.
export function toCsv(rows) {
  const field = (v) => {
    let s = String(v ?? "");
    if (/^[=+\-@\t\r]/.test(s) && !/^-?\d+([.,]\d+)?$/.test(s)) s = `'${s}`;
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return rows.map((r) => r.map(field).join(",")).join("\r\n") + "\r\n";
}
