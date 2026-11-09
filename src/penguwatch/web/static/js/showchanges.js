// Per-show changes made from the library cards, Up Next, Tonight's Pick and the details dialog: each calls the API,
// then tells the page (afterShowChange: "show-updated") so whoever shows that title patches it, instead of reloading
// the whole library. A removed title is "show-removed".
import { api } from "./api.js";
import { emit } from "./bus.js";
import { afterShowChange, scheduleSideRefresh } from "./data.js";
import { libraryLabel } from "./drivers.js";
import { register, registerAction } from "./dispatch.js";
import { state } from "./state.js";
import { displayTitle, householdText, showToast } from "./util.js";

register({
  changeShowStatus, changeShowHousehold, changeShowPriority, changeShowService, changeShowRating, toggleMovieWatched, updateShowProgress,
});
registerAction("delete-show", (el, ev) => {
  const d = el.dataset;
  ev.preventDefault();
  deleteShow(Number(d.id), d.title, d.linked === "true");
});

export async function updateShowProgress(showId, payload) {
  try {
    const updated = await api(`/api/shows/${showId}/progress`, { method: "POST", body: payload });
    showToast(`Updated ${displayTitle(updated)}: ${updated.watched_episodes} eps watched`);
    afterShowChange(updated);
    return updated;
  } catch (err) {
    showToast(err.message, true);
  }
}

async function patchShow(showId, body, message) {
  try {
    const updated = await api(`/api/shows/${showId}`, { method: "PATCH", body });
    if (message) showToast(message);
    afterShowChange(updated);
    return updated;
  } catch (err) {
    showToast(err.message, true);
  }
}

export function changeShowStatus(showId, newStatus) {
  return patchShow(showId, { status: newStatus }, `Status set to ${newStatus}`);
}

// Who wants to watch a title: the ids of the people (at least one). Every "who" control (a Library card, the details
// dialog) sets it through here.
export function changeShowHousehold(showId, ids) {
  return patchShow(showId, { household: ids }, `Who wants it: ${householdText(ids, 60)}`);
}

export function changeShowPriority(showId, priority) {
  return patchShow(showId, { priority }, `Priority: ${priority}`);
}

export function changeShowService(showId, serviceId) {
  const svc = state.services.find((s) => s.id === serviceId);
  return patchShow(showId, { service_id: serviceId || "" }, `Streaming on ${svc ? svc.name : "(unassigned)"}`);
}

export function changeShowRating(showId, value) {
  if (!value) return patchShow(showId, { clear_user_rating: true }, "Rating removed");
  return patchShow(showId, { user_rating: parseInt(value, 10) }, `Rated ${value}/10`);
}

async function toggleMovieWatched(showId, watched) {
  return patchShow(showId, { status: watched ? "completed" : "plantowatch" }, watched ? "Marked watched" : "Marked unwatched");
}

async function deleteShow(showId, title, linked) {
  const msg = linked
    ? `Remove "${title}" from PenguWatch AND from your ${libraryLabel()} lists/history?\n\n(To just stop tracking it, set the status to "Dropped" instead.)`
    : `Remove "${title}" from your tracker?`;
  if (!confirm(msg)) return false;
  try {
    await api(`/api/shows/${showId}`, { method: "DELETE" });
    showToast("Removed");
    emit("show-removed", showId);
    scheduleSideRefresh();
    return true;
  } catch (err) {
    showToast(err.message, true);
    return false;
  }
}
