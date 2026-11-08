// The Settings tab's arithmetic: comparing values, words for values, and the browser-side range checks that read the
// schema (the server checks again with the same ranges). No DOM here.

// A value as JSON with sorted keys, so two equal objects compare equal whatever their key order.
export function canon(v) {
  if (Array.isArray(v)) return `[${v.map(canon).join(",")}]`;
  if (v && typeof v === "object") return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canon(v[k])}`).join(",")}}`;
  return JSON.stringify(v === undefined ? null : v);
}
export const same = (a, b) => canon(a) === canon(b);
// Two lists of items: a field that is null counts as absent (the server fills a saved item's unset fields with null).
const bare = (v) => (Array.isArray(v) ? v.map(bare) : v && typeof v === "object"
  ? Object.fromEntries(Object.entries(v).filter(([, x]) => x !== null && x !== undefined).map(([k, x]) => [k, bare(x)])) : v);
export const sameItems = (a, b) => same(bare(a), bare(b));
export const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));

export const isNumeric = (spec) => spec.type === "number" || spec.type === "integer" || spec.type === "hour";

const tidy = (n) => Number(n.toPrecision(12));
const clock = (h) => `${String(h).padStart(2, "0")}:00`;

// "30 min", "0.5 h", "on"/"off", "09:00", "Me, Partner", "none": the words a row's default line uses.
export function fmt(spec, v, driverLabel = (n) => n) {
  if (v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length)) return spec.empty || "none";
  if (spec.type === "boolean") return v ? "on" : "off";
  if (spec.type === "hour") return clock(v);
  if (spec.type === "choice" || spec.type === "theme") return (spec.choice_labels && spec.choice_labels[v]) || String(v);
  if (spec.type === "driver") return driverLabel(v);
  if (Array.isArray(v)) return v.map((x) => (spec.type === "drivers" ? driverLabel(x) : x)).join(", ");
  if (isNumeric(spec)) return spec.unit ? `${tidy(v)} ${spec.unit}` : String(tidy(v));
  return String(v);
}

// Why `text` (what is typed) is not acceptable for `spec`, in words, or "" when it is.
export function problem(spec, text) {
  const value = String(text ?? "").trim();
  if (value === "") {
    if (spec.nullable) return "";
    return spec.type === "text" && !spec.min_length ? "" : "Enter a value.";
  }
  if (spec.type === "number" || spec.type === "integer") {
    const n = Number(value);
    if (!Number.isFinite(n)) return "Enter a number.";
    if (spec.type === "integer" && !Number.isInteger(n)) return "Enter a whole number.";
    const unit = spec.unit ? ` ${spec.unit}` : "";
    const hasMin = spec.min !== null && spec.min !== undefined;
    const hasMax = spec.max !== null && spec.max !== undefined;
    const low = hasMin && (spec.exclusive_min ? n <= spec.min : n < spec.min);
    const high = hasMax && (spec.exclusive_max ? n >= spec.max : n > spec.max);
    if (low || high) {
      if (hasMin && hasMax) return `Enter a value from ${spec.exclusive_min ? "more than " : ""}${tidy(spec.min)} to ${tidy(spec.max)}${unit}.`;
      return hasMin ? `Enter ${spec.exclusive_min ? "more than" : "at least"} ${tidy(spec.min)}${unit}.` : `Enter at most ${tidy(spec.max)}${unit}.`;
    }
    return "";
  }
  if (spec.type === "text") {
    if (spec.max_length && value.length > spec.max_length) return `At most ${spec.max_length} characters.`;
    if (spec.min_length && value.length < spec.min_length) return `At least ${spec.min_length} character${spec.min_length === 1 ? "" : "s"}.`;
  }
  return "";
}

// The stored value a typed `text` means: a number for a numeric field, else the trimmed text.
export function parse(spec, text) {
  const value = String(text ?? "").trim();
  return spec.type === "number" || spec.type === "integer" ? Number(value) : value;
}

// The person names (household.members): 1 to 2 names of 1 to 40 characters each.
export function namesProblem(spec, names) {
  const list = names.map((n) => String(n).trim());
  const min = spec.min_length || 1;
  const max = spec.max_length || 2;
  if (list.length < min || list.length > max) return `Give ${min === max ? min : `${min} to ${max}`} name${max === 1 ? "" : "s"}.`;
  const bad = list.findIndex((n) => n.length < 1 || n.length > 40);
  return bad >= 0 ? "Each name is 1 to 40 characters." : "";
}

// An id for a new webhook from its name (config.webhook_id): lower case, a-z 0-9 - _, at most 40, "-2", "-3" when taken.
export function webhookId(name, taken = []) {
  const root = (String(name || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^[-_]+|[-_]+$/g, "").slice(0, 36) || "webhook");
  let id = root;
  for (let k = 2; taken.includes(id); k++) id = `${root}-${k}`;
  return id;
}
