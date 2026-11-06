"""The TVmaze driver (https://www.tvmaze.com/api): a public TV catalogue that needs no account or key. It offers the
catalogue (search, details, episode lists, lookup by IMDb / TheTVDB id), the schedule (the next episode of the
library's running shows) and TVmaze's average rating. TV only: no movies, no trending or recommendations, and no
sequels. TVmaze marks anime with the Anime genre; such shows are typed anime.

TVmaze's data is CC BY-SA 4.0 and must be credited: `attribution` is what to show wherever it appears.

This module stays light (the registry imports it): `build` imports the parts that talk to TVmaze."""

from typing import ClassVar, FrozenSet, Tuple

from penguwatch.drivers.base import Attribution, BuiltinRatingSource, Capability
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

    @classmethod
    async def build(cls, repository, options, *, app_version: str, on_request=None):
        from penguwatch.drivers.tvmaze.service import TvmazeService
        return await TvmazeService.create(repository, options, app_version=app_version, on_request=on_request)
