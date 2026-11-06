// The library driver's connection and sync state (/api/drivers/<driver>/status), app-wide: the header's status pill and
// the reconnect banner show it on every tab, and the Services & Sync tab shows its details. A "light" check (the 60 s
// poll, after a change) only refreshes the header unless the full view is open; listeners get { data, light } on
// "library-status".
import { api } from "./api.js";
import { emit } from "./bus.js";
import { loadDrivers } from "./drivers.js";
import { state } from "./state.js";

export async function checkLibraryStatus(light = false) {
  try {
    if (!state.drivers) await loadDrivers();
    const data = await api(`/api/drivers/${encodeURIComponent(state.drivers.library)}/status`);
    state.libraryStatus = data;
    await emit("library-status", { data, light });
  } catch (err) {
    console.error("Error checking the library status:", err);
  }
}
