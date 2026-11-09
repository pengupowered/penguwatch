"""The running TVmaze driver: the client and one object per capability.

- TvmazeCatalog (catalog): search, details, episode lists, and resolving an IMDb or TheTVDB id. TV only; TVmaze has
  no movies, and no sequels (`related` is empty).
- TvmazeSchedule (schedule): the next episode of each running library title.
Ratings travel on Title.ratings (TVmaze's average, 0-10).
"""

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from penguwatch.drivers.base import Episode, RelatedTitle, ScheduledEpisode, Title, state_key
from penguwatch.drivers.tvmaze import mapper
from penguwatch.drivers.tvmaze.client import TvmazeClient, TvmazeError
from penguwatch.models import MediaType
from penguwatch.textsearch import rank_matches

logger = logging.getLogger("penguwatch.drivers.tvmaze")

NAME = mapper.NAME
TV_TYPES = ("show", "anime")
SEARCH_LIMIT = 30
KEY_CURSOR = state_key(NAME, "schedule_cursor")  # the id of the last title the schedule checked


class TvmazeService:
    name = NAME
    label = "TVmaze"

    def __init__(self, client: TvmazeClient, repository, options):
        self.client = client
        self.repository = repository
        self.options = options
        self.library = None
        self.discovery = None
        self.catalog = TvmazeCatalog(self)
        self.schedule = TvmazeSchedule(self)

    @classmethod
    async def create(cls, repository, options, *, app_version: str, on_request=None) -> "TvmazeService":
        client = TvmazeClient(app_version=app_version, timeout_seconds=options.timeout_seconds,
                              rate_calls=options.rate_limit_calls, rate_window=options.rate_window_seconds)
        client.on_request = on_request
        return cls(client, repository, options)

    async def aclose(self) -> None:
        await self.client.aclose()


class TvmazeCatalog:
    """catalog: TVmaze's shows, by TVmaze id."""

    key_provider = NAME

    def __init__(self, service: TvmazeService):
        self.service = service

    async def ready(self) -> bool:
        return True  # no key or login to wait for

    async def search(self, query: str, media_types: Sequence[str], limit: int = SEARCH_LIMIT, detailed: int = 0) -> List[Title]:
        """Shows matching `query`, best match first (textsearch.rank_matches, over everything TVmaze returned); empty when only movies are wanted. (`detailed` is for the other
        catalogues: a TVmaze search result already carries the details.)"""
        wanted = [m for m in media_types if m in TV_TYPES]
        if not wanted:
            return []
        found = await self.service.client.get("/search/shows", {"q": query})
        out: List[Title] = []
        for hit in found if isinstance(found, list) else []:
            title = mapper.show_title(hit.get("show") or {}) if isinstance(hit, dict) else None
            if title and title.media_type in wanted:
                out.append(title)
        return rank_matches(query, out)[:limit]

    async def title(self, media_type: Optional[str], ids: Mapping[str, Any]) -> Optional[Title]:
        """Details by TVmaze id; None when TVmaze has no such show or the type asked for is a movie."""
        tid = ids.get(NAME)
        if not tid or media_type == MediaType.MOVIE.value:
            return None
        show = await self.service.client.get(f"/shows/{int(tid)}", {"embed[]": "akas"}, missing_ok=True)
        if not isinstance(show, dict):
            return None
        akas = (show.get("_embedded") or {}).get("akas")
        return mapper.show_title(show, akas=akas if isinstance(akas, list) else [])

    async def episodes(self, media_type: str, ids: Mapping[str, Any]) -> Optional[List[Episode]]:
        tid = ids.get(NAME)
        if not tid:
            return None
        raw = await self.service.client.get(f"/shows/{int(tid)}/episodes", missing_ok=True)
        return mapper.parse_episodes(raw)  # a show TVmaze dropped has none

    async def related(self, media_type: str, ids: Mapping[str, Any]) -> Optional[List[RelatedTitle]]:
        return []  # TVmaze does not relate shows to each other

    async def resolve(self, media_type: str, ids: Mapping[str, Any]) -> Optional[Tuple[Dict[str, Any], str]]:
        """A TVmaze id for a title known by its IMDb or TheTVDB id (and whether TVmaze files it as anime)."""
        if media_type == MediaType.MOVIE.value:
            return None
        if ids.get(NAME):
            return dict(ids), media_type
        for provider, param in (("imdb", "imdb"), ("tvdb", "thetvdb")):
            if not ids.get(provider):
                continue
            show = await self.service.client.get("/lookup/shows", {param: ids[provider]}, missing_ok=True)
            title = mapper.show_title(show) if isinstance(show, dict) else None
            if title:
                return {**ids, **title.ids}, title.media_type
        return None


class TvmazeSchedule:
    """schedule: the next episode of the running TV titles in the library that TVmaze knows."""

    def __init__(self, service: TvmazeService):
        self.service = service

    async def upcoming(self) -> List[ScheduledEpisode]:
        """At most `schedule_max_titles` titles per run. With more running titles than that, runs take turns: the
        next one starts after the last title the previous one checked (by title id; kept in the driver's own state),
        so every title is reached in time."""
        options = self.service.options
        shows = sorted((s for s in await self.service.repository.get_shows()
                        if s.ids.get(NAME) and s.media_type != MediaType.MOVIE and not s.is_ended), key=lambda s: s.id)
        cap = options.schedule_max_titles
        rotating = len(shows) > cap
        if rotating:
            try:
                after = int(await self.service.repository.get_state(KEY_CURSOR) or 0)
            except ValueError:
                after = 0
            start = next((i for i, s in enumerate(shows) if s.id > after), 0)  # past the last one: round again
            shows = (shows[start:] + shows[:start])[:cap]
        out: List[ScheduledEpisode] = []
        failures = 0
        gave_up = False
        for show in shows:
            try:
                record = await self.service.client.get(f"/shows/{int(show.ids[NAME])}", {"embed": "nextepisode"},
                                                       missing_ok=True)
            except TvmazeError as e:
                logger.debug(f"TVmaze schedule for {show.ids[NAME]} failed: {e}")
                failures += 1
                if failures >= 3:  # TVmaze is down or refusing: do not hammer it
                    gave_up = True
                    break
                continue
            entry = mapper.next_episode(record) if isinstance(record, dict) else None
            if entry:
                out.append(entry)
        if rotating and not gave_up:  # after giving up, the next run tries the same titles again
            await self.service.repository.set_state(KEY_CURSOR, str(shows[-1].id))  # the next run goes on from here
        return out
