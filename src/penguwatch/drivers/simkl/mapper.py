"""SIMKL answers turned into the neutral types of drivers.base: no I/O, no repository access.

Everything here is a plain function over SIMKL JSON (or constants), so it can be tested without a client, a
database or an event loop.
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from penguwatch.db.episodes import format_episode_code
from penguwatch.drivers.base import (
    Episode, LibraryEntry, RatingReading, RelatedTitle, ScheduledEpisode, Title, normal_ids,
)
from penguwatch.drivers.simkl.client import extract_simkl_id, fanart_url, poster_url
from penguwatch.models import MediaType, Show, ShowStatus
from penguwatch.sync.titles import entry_to_show, merge_title, title_names as _title_names

# /sync/all-items/{type} name -> (activities block name, local media type)
SYNC_TYPE_INFO = {
    "shows": ("tv_shows", MediaType.SHOW),
    "anime": ("anime", MediaType.ANIME),
    "movies": ("movies", MediaType.MOVIE),
}
MEDIA_OF = {mt.value: media for media, (_, mt) in SYNC_TYPE_INFO.items()}  # "show" -> "shows"
DETAIL_MEDIA = {"show": "tv", "anime": "anime", "movie": "movies"}  # SIMKL's catalogue path segment
STATUS_BUCKETS = ("watching", "plantowatch", "hold", "completed", "dropped")
STATUS_MAP = {s.value: s for s in ShowStatus}
READ_ONLY_MSG = ("Your SIMKL login is read-only, so changes can't be sent. Click 'Connect SIMKL with Device PIN' "
                 "and approve again to grant write access; queued changes will then go through.")
DEFAULT_RUNTIME = {MediaType.MOVIE: 110, MediaType.ANIME: 24, MediaType.SHOW: 45}
# The external ids a SIMKL record carries that PenguWatch keeps (SIMKL's own first)
KEPT_IDS = ("imdb", "tmdb", "tvdb")


def _int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def item_obj(item: Dict[str, Any]) -> Dict[str, Any]:
    return item.get("show") or item.get("movie") or item.get("anime") or item


def title_ids(obj: Dict[str, Any]) -> Dict[str, Any]:
    """{"simkl": ..., "imdb": ..., "tmdb": ..., "tvdb": ...} from a SIMKL record (those it has)."""
    ids = obj.get("ids") or {}
    return normal_ids({"simkl": extract_simkl_id(obj), "imdb": ids.get("imdb"), "tmdb": _int(ids.get("tmdb")),
                       "tvdb": _int(ids.get("tvdb"))})


def raw_ids(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Like title_ids, with TMDB and TVDB ids as SIMKL wrote them (catalogue details)."""
    ids = obj.get("ids") or {}
    return normal_ids({"simkl": extract_simkl_id(obj), **{k: ids.get(k) for k in KEPT_IDS}})


def extract_watched(item: Dict[str, Any]) -> Optional[List[Tuple[int, int, Optional[str]]]]:
    """Per-episode watched list from an extended=full item, or None if not included."""
    seasons = item.get("seasons")
    if not isinstance(seasons, list):
        return None
    out = []
    for s in seasons:
        snum = s.get("number")
        for ep in s.get("episodes") or []:
            if snum is None or ep.get("number") is None:
                continue
            out.append((int(snum), int(ep["number"]), ep.get("watched_at")))
    return out


# ---------------------------------------------------------------- library entries (/sync/all-items, /sync/ratings)


def _infer_media_type(item: Dict[str, Any], is_anime: bool, media_type: Optional[MediaType]) -> MediaType:
    if media_type is not None:
        return media_type
    if item.get("movie"):
        return MediaType.MOVIE
    if is_anime or item.get("anime"):
        return MediaType.ANIME
    return MediaType.SHOW


def _parse_genres(obj: Dict[str, Any], media_type: MediaType) -> List[str]:
    genres = obj.get("genres") or []
    if isinstance(genres, str):
        genres = [g.strip() for g in genres.split(",") if g.strip()]
    genres = list(genres)
    if media_type == MediaType.ANIME and "Anime" not in genres:
        genres.append("Anime")
    return genres


def _parse_ratings(obj: Dict[str, Any], media_type: MediaType) -> Tuple[Any, Any, Any]:
    """(simkl_rating, imdb_rating, imdb_votes); anime without IMDb falls back to MAL, then SIMKL."""
    ratings = obj.get("ratings") or {}
    simkl_rating = imdb_rating = imdb_votes = None
    if isinstance(ratings, dict):
        if isinstance(ratings.get("simkl"), dict):
            simkl_rating = ratings["simkl"].get("rating")
        if isinstance(ratings.get("imdb"), dict):
            imdb_rating = ratings["imdb"].get("rating")
            imdb_votes = ratings["imdb"].get("votes")
        if media_type == MediaType.ANIME and not imdb_rating:
            if isinstance(ratings.get("mal"), dict):
                imdb_rating = ratings["mal"].get("rating")
            elif simkl_rating:
                imdb_rating = simkl_rating
    return simkl_rating, imdb_rating, imdb_votes


def _movie_progress(obj: Dict[str, Any], status: ShowStatus) -> Dict[str, Any]:
    return {
        "total_episodes": 1,
        "aired_episodes": 1,
        "watched_episodes": 1 if status == ShowStatus.COMPLETED else 0,
        "runtime_minutes": _int(obj.get("runtime")) or 110,
        "next_to_watch": None,
        "last_watched": None,
        "is_ended": True,
    }


def _aired_episodes(item: Dict[str, Any], obj: Dict[str, Any], total_episodes: int) -> int:
    if item.get("not_aired_episodes_count") is not None:
        return max(0, total_episodes - item["not_aired_episodes_count"])
    if obj.get("aired_episodes") is not None:
        return obj["aired_episodes"]
    return total_episodes


def _next_to_watch(item: Dict[str, Any]) -> Optional[str]:
    nti = item.get("next_to_watch_info") or {}
    if nti.get("season") is not None and nti.get("episode") is not None:
        return format_episode_code(nti["season"], nti["episode"])
    return item.get("next_to_watch")


def _series_progress(item: Dict[str, Any], obj: Dict[str, Any], media_type: MediaType) -> Dict[str, Any]:
    total_episodes = (
        item.get("total_episodes_count") or obj.get("total_episodes") or obj.get("episodes_count") or 0
    )
    return {
        "total_episodes": total_episodes,
        "aired_episodes": _aired_episodes(item, obj, total_episodes),
        "watched_episodes": item.get("watched_episodes_count") or 0,
        "runtime_minutes": _int(obj.get("runtime")) or (24 if media_type == MediaType.ANIME else 45),
        "last_watched": item.get("last_watched"),
        "next_to_watch": _next_to_watch(item),
        "is_ended": str(obj.get("status", "")).lower() == "ended",
    }


def parse_entry(item: Dict[str, Any], is_anime: bool = False,
                media_type: Optional[MediaType] = None) -> Optional[LibraryEntry]:
    """A library entry from /sync/all-items (or a catalogue/search record); None without a title."""
    media_type = _infer_media_type(item, is_anime, media_type)
    obj = item_obj(item)
    if not obj or not obj.get("title"):
        return None
    simkl_rating, imdb_rating, imdb_votes = _parse_ratings(obj, media_type)
    ratings: Dict[str, RatingReading] = {"simkl": RatingReading(simkl_rating),
                                         "imdb": RatingReading(imdb_rating, imdb_votes)}
    status = STATUS_MAP.get(item.get("status") or "", ShowStatus.PLANTOWATCH)
    progress = _movie_progress(obj, status) if media_type == MediaType.MOVIE else _series_progress(item, obj, media_type)
    title = Title(
        media_type=media_type.value, title=obj["title"], ids=title_ids(obj), en_title=obj.get("en_title"),
        alt_titles=obj.get("alt_titles"), year=_int(obj.get("year")), poster=poster_url(obj.get("poster")),
        banner=fanart_url(obj.get("fanart")), overview=obj.get("overview"), genres=_parse_genres(obj, media_type),
        network=obj.get("network"), ratings=ratings,
    )
    return LibraryEntry(
        title=title, status=status.value, has_rating="user_rating" in item, user_rating=item.get("user_rating"),
        watched=extract_watched(item), watched_count=progress["watched_episodes"],
        total_episodes=progress["total_episodes"], aired_episodes=progress["aired_episodes"],
        last_watched=progress["last_watched"], next_to_watch=progress["next_to_watch"],
        last_watched_at=item.get("last_watched_at"), is_ended=progress["is_ended"],
        runtime=progress["runtime_minutes"],
    )


def parse_show_item(item: Dict[str, Any], is_anime: bool = False,
                    media_type: Optional[MediaType] = None) -> Optional[Show]:
    """The Show a library entry describes (parse_entry, then the generic sync.titles.entry_to_show)."""
    entry = parse_entry(item, is_anime=is_anime, media_type=media_type)
    return entry_to_show(entry) if entry else None


# ---------------------------------------------------------------- catalogue: details, episodes, relations


def extract_sequels(details: Dict[str, Any]) -> List[RelatedTitle]:
    """Direct sequels from an anime's SIMKL relations."""
    out = []
    for rel in details.get("relations") or []:
        if rel.get("relation_type") != "sequel" or not rel.get("is_direct"):
            continue
        if rel.get("anime_type") not in (None, "tv", "ona", "ova", "movie"):
            continue
        sid = extract_simkl_id(rel)
        if sid:
            out.append(RelatedTitle(ids={"simkl": sid}, title=rel.get("en_title") or rel.get("title"),
                                    year=rel.get("year"), poster=poster_url(rel.get("poster")),
                                    kind=rel.get("anime_type")))
    return out


def _detail_genres(d: Dict[str, Any]) -> List[str]:
    g = d.get("genres") or []
    if isinstance(g, str):
        g = [x.strip() for x in g.split(",")]
    return list(g)


def details_title(details: Dict[str, Any], media_type: MediaType) -> Optional[Title]:
    """A title's SIMKL details (GET /{tv|anime|movies}/{id}); None when there are none."""
    if not details:
        return None
    ratings: Dict[str, RatingReading] = {}
    r = details.get("ratings")
    r = r if isinstance(r, dict) else {}
    if isinstance(r.get("imdb"), dict) and r["imdb"].get("rating"):
        ratings["imdb"] = RatingReading(r["imdb"]["rating"], r["imdb"].get("votes"))
    if isinstance(r.get("simkl"), dict) and r["simkl"].get("rating"):
        ratings["simkl"] = RatingReading(r["simkl"]["rating"])
    if media_type == MediaType.ANIME and "imdb" not in ratings and isinstance(r.get("mal"), dict) \
            and r["mal"].get("rating"):
        ratings["imdb"] = RatingReading(r["mal"]["rating"], fallback=True)  # the IMDb source holds MAL for anime
    return Title(
        media_type=media_type.value, title=details.get("title") or "", ids=raw_ids(details),
        en_title=details.get("en_title"), alt_titles=details.get("alt_titles"),
        year=_int(details.get("year")), poster=poster_url(details.get("poster")),
        banner=fanart_url(details.get("fanart")), overview=details.get("overview"), genres=_detail_genres(details),
        network=details.get("network"), runtime=_int(details.get("runtime")), status=details.get("status"),
        total_episodes=details.get("total_episodes"), ratings=ratings,
        related=extract_sequels(details) if media_type == MediaType.ANIME else None,
        extra={"record": details},
    )


def merge_details(show: Show, details: Dict[str, Any]) -> Dict[str, Any]:
    """The columns SIMKL details should change on `show` (details_title, then the generic merge)."""
    return merge_title(show, details_title(details, show.media_type))


def title_names(show: Show, details: Dict[str, Any]) -> Optional[Tuple[Optional[str], List[str]]]:
    return _title_names(show, details_title(details, show.media_type))


def parse_episodes(raw: List[Dict[str, Any]], media: str) -> List[Episode]:
    """SIMKL's episode list (media: tv | anime): episodes only, numbered seasons (anime without one: season 1)."""
    eps = []
    for ep in raw:
        if ep.get("type") not in (None, "episode"):
            continue
        season = ep.get("season")
        number = ep.get("episode") if ep.get("episode") is not None else ep.get("number")
        if season is None and media == "anime":
            season = 1
        if season is None or number is None or int(season) <= 0:
            continue
        eps.append(Episode(season=int(season), number=int(number), title=ep.get("title"),
                           air_date=(ep.get("date") or "")[:10] or None,
                           ids=normal_ids({"simkl": (ep.get("ids") or {}).get("simkl_id")})))
    return eps


# ---------------------------------------------------------------- catalogue records: search, trending, related


def _catalog_ratings(obj: Dict[str, Any]) -> Dict[str, RatingReading]:
    ratings = obj.get("ratings") or {}
    out: Dict[str, RatingReading] = {}
    if isinstance(ratings.get("imdb"), dict):
        out["imdb"] = RatingReading(ratings["imdb"].get("rating"), ratings["imdb"].get("votes"))
    if isinstance(ratings.get("simkl"), dict):
        out["simkl"] = RatingReading(ratings["simkl"].get("rating"))
    return out


def catalog_item(raw: Dict[str, Any], media_type: str) -> Optional[Title]:
    """A trending or recommendation record; None without a title. Title.extra has rank and watchers."""
    obj = item_obj(raw)
    if not obj.get("title"):
        return None
    year = obj.get("year")
    if not year and obj.get("release_date"):
        m = re.search(r"(19|20)\d{2}", str(obj["release_date"]))  # trending files use MM/DD/YYYY
        year = m.group(0) if m else None
    ids = obj.get("ids") or {}
    try:
        tmdb_id = int(ids["tmdb"]) if ids.get("tmdb") else None
    except (TypeError, ValueError):
        tmdb_id = None
    return Title(
        media_type=media_type, title=obj.get("title"), ids=normal_ids({"simkl": extract_simkl_id(obj), "tmdb": tmdb_id}),
        year=_int(year), poster=poster_url(obj.get("poster")), overview=obj.get("overview"),
        network=obj.get("network"), genres=obj.get("genres") or [], ratings=_catalog_ratings(obj),
        extra={"rank": raw.get("rank") or obj.get("rank"), "watchers": raw.get("watched") or obj.get("watched")},
    )


def search_title(item: Dict[str, Any]) -> Title:
    """A search result (SimklClient.search, already given 'media_type' and SIMKL's id in 'ids'). Title.extra has
    the record itself under "record" (what library.merge_details reads) and its raw fields."""
    ratings = _catalog_ratings(item)
    mal = (item.get("ratings") or {}).get("mal")
    if item.get("media_type") == "anime" and not (ratings.get("imdb") and ratings["imdb"].value) \
            and isinstance(mal, dict) and mal.get("rating"):
        ratings["imdb"] = RatingReading(mal["rating"], fallback=True)  # the IMDb source holds MAL for anime
    return Title(
        media_type=item["media_type"], title=item.get("title") or "", ids=normal_ids(item.get("ids") or {}),
        en_title=item.get("en_title") or item.get("title_en"), alt_titles=item.get("alt_titles"),
        year=_int(item.get("year")), poster=poster_url(item.get("poster")), banner=fanart_url(item.get("fanart")),
        overview=item.get("overview"), genres=_detail_genres(item), network=item.get("network"),
        runtime=_int(item.get("runtime")),
        status=item.get("status"), total_episodes=item.get("total_episodes"), ratings=ratings,
        extra={"record": item},
    )


# ---------------------------------------------------------------- schedule


def schedule_entries(airing: List[Dict[str, Any]], calendar: Dict[str, Any]) -> List[ScheduledEpisode]:
    """Upcoming episodes from the airing lists and the user's calendar."""
    entries: List[ScheduledEpisode] = []
    for item in airing:
        sid = extract_simkl_id(item)
        ep = item.get("episode") or {}
        if sid and item.get("date"):
            entries.append(ScheduledEpisode(media_type=None, ids={"simkl": sid}, season=ep.get("season") or 1,
                                            number=ep.get("episode") or 1, title=ep.get("title"),
                                            air_date=item["date"][:10]))
    for day_obj in (calendar.get("new_episodes") or {}).values():
        if not isinstance(day_obj, dict):
            continue
        air = None
        if day_obj.get("day_ts"):
            try:
                air = datetime.fromtimestamp(day_obj["day_ts"], timezone.utc).date().isoformat()
            except Exception:
                air = None
        for item in (day_obj.get("items") or {}).get("all", []):
            sid = extract_simkl_id(item)
            if sid and air:
                entries.append(ScheduledEpisode(media_type=None, ids={"simkl": sid}, season=item.get("season") or 1,
                                                number=item.get("episode") or 1, title=item.get("title"),
                                                air_date=air))
    return entries
