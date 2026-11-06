// Tonight's pick (decision roulette).
import { api } from "../../js/api.js";
import { changeShowStatus } from "../../js/showchanges.js";
import { PLACEHOLDER_POSTER, displayTitle, householdLabel, ratingBadges, showToast } from "../../js/util.js";

// --- Tonight's Pick ---

function setupDecisionRoulette() {
  const btnSpin = document.getElementById("btn-spin-roulette");
  const btnSpinAgain = document.getElementById("btn-spin-again");
  const btnStartWatching = document.getElementById("btn-result-start-watching");
  let startWatching = null;  // what "Start watching" does for the current pick
  btnStartWatching.addEventListener("click", () => startWatching?.());

  async function pickShow() {
    const payload = {
      only_active_services: document.getElementById("decide-service").value === "active",
      min_rating: parseFloat(document.getElementById("decide-rating").value) || 0,
      ready_to_binge_only: document.getElementById("decide-ready-binge").checked,
    };
    const audience = document.getElementById("decide-audience").value;
    if (audience !== "all") payload.household_tag = audience;
    const media = document.getElementById("decide-media").value;
    if (media) payload.media_type = media;

    try {
      const data = await api("/api/decide/pick", { method: "POST", body: payload });
      if (!data.result) {
        showToast(data.message || "No titles matched your criteria", true);
        return;
      }
      const show = data.result;
      const isMovie = show.media_type === "movie";
      document.getElementById("result-title").textContent = displayTitle(show);
      document.getElementById("result-subtitle").textContent = displayTitle(show) !== show.title ? show.title : "";
      document.getElementById("result-year").textContent = show.year ? `(${show.year})` : "";
      document.getElementById("result-ratings-wrap").innerHTML = ratingBadges(show);
      document.getElementById("result-network").textContent = show.network || "Stream";
      document.getElementById("result-household").textContent = householdLabel(show.household_tag);
      document.getElementById("result-binge").hidden = !(show.is_ready_to_binge && !isMovie);
      document.getElementById("result-overview").textContent = show.overview || "No overview available.";
      document.getElementById("result-progress-text").textContent = isMovie
        ? `Movie · ${show.runtime_minutes} min`
        : `Watched ${show.watched_episodes} of ${show.aired_episodes || show.total_episodes} episodes${show.next_to_watch ? ` · next up ${show.next_to_watch}` : ""}`;
      document.getElementById("result-poster").src = show.poster || PLACEHOLDER_POSTER;
      document.getElementById("decide-result").hidden = false;
      document.getElementById("decide-result").scrollIntoView({ behavior: "smooth" });
      btnStartWatching.querySelector("span").textContent = isMovie ? "Watching it tonight" : "Start watching tonight";
      startWatching = async () => {
        if (!isMovie) await changeShowStatus(show.id, "watching");
        showToast(`Enjoy ${displayTitle(show)}!`);
      };
    } catch (err) {
      showToast(err.message, true);
    }
  }

  btnSpin.addEventListener("click", pickShow);
  btnSpinAgain.addEventListener("click", pickShow);
}

export default { id: "decide", mount: setupDecisionRoulette };
