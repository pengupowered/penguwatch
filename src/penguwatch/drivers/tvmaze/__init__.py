"""The TVmaze driver (https://www.tvmaze.com/api): a public TV catalogue that needs no account or key. It offers the
catalogue (search, details, episode lists, lookup by IMDb / TheTVDB id), the schedule (the next episode of the
library's running shows) and TVmaze's average rating. TV only: no movies, no trending or recommendations, and no
sequels. TVmaze marks anime with the Anime genre; such shows are typed anime.

TVmaze's data is CC BY-SA 4.0 and must be credited: `attribution` is what to show wherever it appears.

This module stays light (the registry imports it): `build` imports the parts that talk to TVmaze."""

from typing import Any, ClassVar, FrozenSet, Mapping, Tuple

from penguwatch.drivers.base import Attribution, BuiltinRatingSource, Capability, DriverSetting
from penguwatch.drivers.tvmaze.options import TvmazeOptions


class TvmazeDriver:
    name: ClassVar[str] = "tvmaze"
    label: ClassVar[str] = "TVmaze"
    offers: ClassVar[FrozenSet[Capability]] = frozenset({Capability.CATALOG, Capability.SCHEDULE, Capability.RATINGS})
    options_model: ClassVar[type] = TvmazeOptions
    # No column in shows: the value is stored in title_ratings, with the link to the show's TVmaze page
    rating_sources: ClassVar[Tuple[BuiltinRatingSource, ...]] = (
        BuiltinRatingSource(id="tvmaze", name="TVmaze", label="TVmaze", show_on_cards=False),
    )
    secret_state_keys: ClassVar[Tuple[str, ...]] = ()
    health_url: ClassVar[str] = "https://api.tvmaze.com/"  # what `penguwatch doctor` checks is reachable
    attribution: ClassVar[Attribution] = Attribution(text="TV data from TVmaze", url="https://www.tvmaze.com")
    # The Settings tab (Advanced, group "TVmaze"); the client is built once with the last three
    settings: ClassVar[Tuple[DriverSetting, ...]] = (
        DriverSetting("schedule_max_titles", "advanced", "Titles checked per schedule run",
                      "How many library titles the TVmaze schedule looks up each run (one request each).",
                      unit="titles"),
        DriverSetting("rate_limit_calls", "advanced", "Requests per window",
                      "TVmaze allows at least 20 per 10 seconds per address; stay a little under.", unit="requests",
                      live=False),
        DriverSetting("rate_window_seconds", "advanced", "Rate window", "The window the request limit counts in.",
                      unit="s", live=False),
        DriverSetting("timeout_seconds", "advanced", "Request timeout",
                      "A TVmaze request without an answer in this time fails.", unit="s", live=False),
    )
    not_on_page: ClassVar[Mapping[str, str]] = {}
    # 0.15 / 80aebed defaults (frozen)
    legacy_defaults: ClassVar[Mapping[str, Any]] = {
        "schedule_max_titles": 60, "rate_limit_calls": 18, "rate_window_seconds": 10.0, "timeout_seconds": 20.0,
    }

    @classmethod
    async def build(cls, repository, options, *, app_version: str, on_request=None):
        from penguwatch.drivers.tvmaze.service import TvmazeService
        return await TvmazeService.create(repository, options, app_version=app_version, on_request=on_request)
