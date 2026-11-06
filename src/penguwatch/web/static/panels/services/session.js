// Sign out (homeguard's POST /logout): the Services & Sync tab's sign-in card. /api/session says whether this browser
// signed in with the access token's cookie (only then is there a sign-in to end) and whether "sign out everywhere" is
// available. The card's forms post to /logout themselves; this module only shows the card and the confirmation step.
import { api } from "../../js/api.js";
import { register } from "../../js/dispatch.js";

function confirming(on) {
  const ask = document.getElementById("btn-sign-out-everywhere");
  document.getElementById("signout-everywhere-confirm").hidden = !on;
  ask.setAttribute("aria-expanded", String(on));
  ask.disabled = on;
  (on ? document.getElementById("btn-sign-out-everywhere-confirm") : ask).focus();
}

export async function setupSession() {
  let session;
  try {
    session = await api("/api/session");
  } catch {
    return; // not signed in, or offline: nothing to offer
  }
  document.getElementById("signout-card").hidden = !session.sign_out;
  document.getElementById("signout-everywhere-row").hidden = !session.sign_out_everywhere;
}

register({
  askSignOutEverywhere: () => confirming(true),
  cancelSignOutEverywhere: () => confirming(false),
});
