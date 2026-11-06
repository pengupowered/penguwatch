"""Rules for turning a create request plus a catalogue's details (drivers.base.Title) into a Show.

Everything here is pure (no I/O, no global state), so each rule can be tested on its own.
The API layer (api/titles.py) does the lookups and persistence and calls these in order:

    search_media_types -> pick_search_match  (find the catalogue's title from a name)
    merge_details                            (request fields + catalogue details -> one set of fields)
    assemble_show                            (media-type rules, defaults, final Show)
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from penguwatch import clock
from penguwatch.drivers.base import Title
from penguwatch.models import MediaType, Show, ShowCreateRequest, ShowStatus
from penguwatch.ratings import builtin_rating_columns
from penguwatch.textsearch import clean_alt_titles, clean_en_title

DEFAULT_RUNTIME = {MediaType.MOVIE: 110, MediaType.ANIME: 24, MediaType.SHOW: 45}


def search_media_types(media_type: Optional[MediaType]) -> Tuple[str, ...]:
    """What to search for a requested type: a series may be filed as anime."""
    return {"movie": ("movie",), "anime": ("anime",), "show": ("show", "anime")}.get(
        media_type.value if media_type else "", ("show", "anime", "movie")
    )


def _year(match: Any) -> Optional[int]:
    year = match.get("year") if isinstance(match, dict) else getattr(match, "year", None)
    return int(year) if year else None


def pick_search_match(matches: Sequence[Any], year: Optional[int]) -> Optional[Any]:
    """First result, preferring ones within a year of the requested year."""
    if year:
        near = [m for m in matches if not _year(m) or abs(_year(m) - year) <= 1]
        matches = near or list(matches)
    return matches[0] if matches else None


def parse_genres(raw: Any) -> List[str]:
    if isinstance(raw, str):
        return [g.strip() for g in raw.split(",") if g.strip()]
    return list(raw or [])


def parse_runtime(raw: Any) -> Optional[int]:
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def parse_tmdb_id(ids: Dict[str, Any], current: Optional[int]) -> Optional[int]:
    if current:
        return current
    try:
        return int(ids["tmdb"]) if ids.get("tmdb") else None
    except (TypeError, ValueError):
        return current


def extract_ratings(
    title: Title, media_type: MediaType, imdb_rating: Optional[float]
) -> Tuple[Optional[float], Optional[float], Optional[int]]:
    """Returns (imdb_rating, simkl_rating, imdb_votes): the built-in sources' columns. Anime without an IMDb score
    takes the stand-in the catalogue gives for it (MyAnimeList), else the other built-in score."""
    columns = builtin_rating_columns()
    values: Dict[str, Any] = {}
    stand_ins: Dict[str, Any] = {}
    for source, reading in title.ratings.items():
        if source not in columns:
            continue
        column, votes_column = columns[source]
        if reading.fallback:
            stand_ins[column] = reading.value
            continue
        values[column] = reading.value
        if votes_column:
            values[votes_column] = reading.votes
    imdb_rating = values.get("imdb_rating") or imdb_rating
    votes = values.get("imdb_votes")
    simkl_rating = values.get("simkl_rating")
    if media_type == MediaType.ANIME and not imdb_rating:
        imdb_rating = stand_ins.get("imdb_rating") or simkl_rating
    return imdb_rating, simkl_rating, votes


@dataclass
class Merged:
    """Request fields overlaid with whatever the catalogue knew."""

    title: str
    en_title: Optional[str] = None
    alt_titles: List[str] = field(default_factory=list)
    year: Optional[int] = None
    overview: Optional[str] = None
    network: Optional[str] = None
    poster: Optional[str] = None
    banner: Optional[str] = None
    genres: List[str] = field(default_factory=list)
    imdb_rating: Optional[float] = None
    simkl_rating: Optional[float] = None
    imdb_votes: Optional[int] = None
    total_episodes: int = 0
    aired_episodes: int = 0
    ids: Dict[str, Any] = field(default_factory=dict)
    runtime: Optional[int] = None
    is_ended: bool = False
    enriched: bool = False


def merge_details(payload: ShowCreateRequest, details: Optional[Title], media_type: MediaType) -> Merged:
    m = Merged(
        title=payload.title, year=payload.year, overview=payload.overview, network=payload.network,
        poster=payload.poster, banner=payload.banner, genres=list(payload.genres or []),
        imdb_rating=payload.imdb_rating, total_episodes=payload.total_episodes,
        aired_episodes=payload.aired_episodes, ids=dict(payload.ids),
    )
    if not details:
        return m
    m.enriched = True
    m.title = details.title or m.title
    m.en_title = clean_en_title(m.title, details.en_title)
    m.alt_titles = clean_alt_titles(details.alt_titles)
    m.year = details.year or m.year
    m.overview = details.overview or m.overview
    m.network = details.network or m.network
    m.poster = details.poster or m.poster
    m.banner = details.banner or m.banner
    m.genres = list(dict.fromkeys(m.genres + [g for g in details.genres if g]))
    if details.total_episodes:
        m.total_episodes = m.aired_episodes = details.total_episodes
    m.runtime = parse_runtime(details.runtime)
    m.is_ended = str(details.status or "").lower() == "ended"
    ids = details.ids
    if not m.ids.get("imdb") and ids.get("imdb"):
        m.ids["imdb"] = ids["imdb"]
    tmdb = parse_tmdb_id(ids, m.ids.get("tmdb"))
    if tmdb:
        m.ids["tmdb"] = tmdb
    m.imdb_rating, m.simkl_rating, m.imdb_votes = extract_ratings(details, media_type, m.imdb_rating)
    return m


def normalize_status(media_type: MediaType, status: ShowStatus, watched: int) -> Tuple[ShowStatus, int]:
    """Movies are never 'watching' or 'on hold', and their progress is 0 or 1."""
    if media_type == MediaType.MOVIE:
        if status in (ShowStatus.WATCHING, ShowStatus.HOLD):
            status = ShowStatus.PLANTOWATCH
        watched = 1 if status == ShowStatus.COMPLETED else 0
    return status, watched


def assemble_show(
    payload: ShowCreateRequest,
    merged: Merged,
    *,
    ids: Dict[str, Any],
    media_type: MediaType,
    service_id: Optional[str],
    network: Optional[str],
) -> Show:
    genres = list(merged.genres)
    total, aired, ended = merged.total_episodes, merged.aired_episodes, merged.is_ended
    if media_type == MediaType.ANIME and "Anime" not in genres:
        genres.append("Anime")
    if media_type == MediaType.MOVIE:
        total = aired = 1
        ended = True
    status, watched = normalize_status(media_type, payload.status, payload.watched_episodes)
    return Show(
        title=merged.title, en_title=merged.en_title, alt_titles=merged.alt_titles, year=merged.year, ids={**ids, **{k: v for k, v in merged.ids.items() if k not in ids}},
        media_type=media_type, network=network, service_id=service_id, service_manual=bool(payload.service_id),
        status=status, priority=payload.priority, household_tag=payload.household_tag,
        overview=merged.overview, poster=merged.poster, banner=merged.banner, genres=genres,
        imdb_rating=merged.imdb_rating, imdb_votes=merged.imdb_votes, simkl_rating=merged.simkl_rating,
        total_episodes=total, aired_episodes=aired, watched_episodes=watched,
        runtime_minutes=merged.runtime or DEFAULT_RUNTIME[media_type],
        is_ended=ended,
        enriched_at=clock.utc_now().isoformat() if merged.enriched else None,
    )
