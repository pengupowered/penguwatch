"""Neutral titles (drivers.base) turned into what the library stores: a library entry into a Show, catalogue
details into the columns enrichment changes. Pure functions: no I/O."""

import json
from typing import Any, Dict, List, Optional, Tuple

from penguwatch.drivers.base import LibraryEntry, RelatedTitle, Title
from penguwatch.models import HouseholdTag, MediaType, Priority, Show, ShowStatus
from penguwatch.ratings import STORED_BUILTIN_IDS, RatingValue, builtin_rating_columns
from penguwatch.textsearch import clean_alt_titles, clean_en_title


def rating_fields(title: Title) -> Dict[str, Any]:
    """The built-in rating columns a title's readings fill (fallback readings included)."""
    columns = builtin_rating_columns()
    fields: Dict[str, Any] = {}
    for source, reading in title.ratings.items():
        if source not in columns:
            continue
        column, votes_column = columns[source]
        fields[column] = reading.value
        if votes_column:
            fields[votes_column] = reading.votes
    return fields


def stored_ratings(title: Title) -> Dict[str, RatingValue]:
    """The readings for built-in sources that have no column (stored in title_ratings), with the link to the title's
    page at the service when the catalogue gives one (Title.extra["url"])."""
    url = title.extra.get("url")
    return {source: RatingValue(value=reading.value, votes=reading.votes, url=url if isinstance(url, str) else None)
            for source, reading in title.ratings.items()
            if source in STORED_BUILTIN_IDS and not reading.fallback and reading.value}


def entry_to_show(entry: LibraryEntry) -> Show:
    """The Show a library entry describes (household fields at their defaults)."""
    t = entry.title
    return Show(
        ids=t.ids,
        media_type=MediaType(t.media_type),
        title=t.title,
        en_title=clean_en_title(t.title, t.en_title),
        alt_titles=clean_alt_titles(t.alt_titles),
        year=t.year,
        poster=t.poster,
        banner=t.banner,
        overview=t.overview,
        genres=list(t.genres),
        network=t.network,
        status=ShowStatus(entry.status),
        priority=Priority.MEDIUM,
        household_tag=HouseholdTag.PERSON1,
        **rating_fields(t),
        user_rating=entry.user_rating,
        last_watched_at=entry.last_watched_at,
        total_episodes=entry.total_episodes,
        aired_episodes=entry.aired_episodes,
        watched_episodes=entry.watched_count,
        runtime_minutes=entry.runtime,
        last_watched=entry.last_watched,
        next_to_watch=entry.next_to_watch,
        is_ended=entry.is_ended,
    )


def sequel_record(related: RelatedTitle) -> Dict[str, Any]:
    """How a sequel is stored (shows.sequels_json) and shown: {ids, title, year, poster, anime_type}."""
    return {"ids": dict(related.ids), "title": related.title, "year": related.year, "poster": related.poster,
            "anime_type": related.kind}


def _text_fields(show: Show, t: Title) -> Dict[str, Any]:
    """Network (always) plus overview / banner / poster (only when the show has none)."""
    fields: Dict[str, Any] = {}
    if t.network:
        fields["network"] = t.network
    if t.overview and not show.overview:
        fields["overview"] = t.overview
    if t.banner and not show.banner:
        fields["banner"] = t.banner
    if t.poster and not show.poster:
        fields["poster"] = t.poster
    return fields


def _run_info(show: Show, t: Title) -> Dict[str, Any]:
    """Runtime, ended flag and episode totals."""
    fields: Dict[str, Any] = {}
    if t.runtime:
        fields["runtime_minutes"] = t.runtime
    if t.status and show.media_type != MediaType.MOVIE:
        fields["is_ended"] = 1 if str(t.status).lower() == "ended" else 0
    if show.total_episodes == 0 and t.total_episodes:
        fields["total_episodes"] = t.total_episodes
        fields["aired_episodes"] = t.total_episodes
    return fields


def _genres_and_ids(show: Show, t: Title) -> Dict[str, Any]:
    """Genres merged with the existing ones, and external ids the show lacks."""
    fields: Dict[str, Any] = {}
    if t.genres:
        fields["genres_json"] = json.dumps(list(dict.fromkeys(list(show.genres) + list(t.genres))))
    missing = {k: v for k, v in t.ids.items() if k not in show.ids}
    if missing:
        fields["ids"] = missing
    return fields


def _ratings(show: Show, t: Title) -> Dict[str, Any]:
    """The built-in rating sources' values; a fallback reading only where the show has no value."""
    columns = builtin_rating_columns()
    fields: Dict[str, Any] = {}
    for source, reading in t.ratings.items():
        if reading.fallback or source not in columns or not reading.value:
            continue
        column, votes_column = columns[source]
        fields[column] = reading.value
        if votes_column:
            fields[votes_column] = reading.votes
    for source, reading in t.ratings.items():
        if not reading.fallback or source not in columns or not reading.value:
            continue
        column, _ = columns[source]
        if column not in fields and not getattr(show, column, None):
            fields[column] = reading.value
    return fields


def merge_title(show: Show, title: Optional[Title]) -> Dict[str, Any]:
    """The columns a catalogue's details of `show` should change (empty when there are none)."""
    if title is None:
        return {}
    fields: Dict[str, Any] = {}
    fields.update(_text_fields(show, title))
    fields.update(_run_info(show, title))
    fields.update(_genres_and_ids(show, title))
    fields.update(_ratings(show, title))
    if show.media_type == MediaType.ANIME and title.related is not None:
        fields["sequels_json"] = json.dumps([sequel_record(r) for r in title.related])
    return fields


def title_names(show: Show, title: Optional[Title]) -> Optional[Tuple[Optional[str], List[str]]]:
    """(English title, alternative titles) from a catalogue's details; None when there are none, so the title stays
    "not checked". The English title is None when it only restates the title."""
    if title is None:
        return None
    return clean_en_title(show.title, title.en_title), clean_alt_titles(title.alt_titles)
