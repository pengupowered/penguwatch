"""TVmaze's answers turned into the neutral types of drivers.base. Pure functions; a field TVmaze did not report
stays None (or empty), never guessed."""

import html
import re
from typing import Any, Dict, List, Optional

from penguwatch.drivers.base import Episode, Ids, RatingReading, ScheduledEpisode, Title, normal_ids

NAME = "tvmaze"
SOURCE = "tvmaze"  # the id of TVmaze's built-in rating source
_TAGS = re.compile(r"<[^>]+>")
_BREAKS = re.compile(r"</p>|<br\s*/?>", re.IGNORECASE)


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def plain_text(summary: Any) -> Optional[str]:
    """TVmaze's HTML summary as plain text (paragraphs joined by a space); None when empty."""
    if not isinstance(summary, str):
        return None
    text = html.unescape(_TAGS.sub("", _BREAKS.sub(" ", summary)))
    text = " ".join(text.split())
    return text or None


def media_type_of(show: Dict[str, Any]) -> str:
    """TVmaze files everything as a TV show; the Anime genre is how it marks anime."""
    genres = show.get("genres") or []
    return "anime" if any(isinstance(g, str) and g.lower() == "anime" for g in genres) else "show"


def show_ids(show: Dict[str, Any]) -> Ids:
    """TVmaze's own id and the cross ids it reports (imdb, tvdb)."""
    ext = show.get("externals") or {}
    return normal_ids({NAME: show.get("id"), "imdb": ext.get("imdb"), "tvdb": ext.get("thetvdb")})


def _image(show: Dict[str, Any]) -> Optional[str]:
    image = show.get("image")
    url = image.get("medium") or image.get("original") if isinstance(image, dict) else None
    return url if isinstance(url, str) and url.startswith("http") else None


def _network(show: Dict[str, Any]) -> Optional[str]:
    for key in ("network", "webChannel"):
        channel = show.get(key)
        if isinstance(channel, dict) and channel.get("name"):
            return str(channel["name"])
    return None


def _status(show: Dict[str, Any]) -> Optional[str]:
    status = show.get("status")
    return str(status).strip().lower() or None if isinstance(status, str) else None


def show_title(show: Dict[str, Any], akas: Optional[List[Dict[str, Any]]] = None) -> Optional[Title]:
    """The Title for a TVmaze show record; None when it has no id or name. `akas` (TVmaze's other names) makes
    alt_titles a list; None leaves it "not asked"."""
    ids = show_ids(show)
    name = show.get("name")
    if NAME not in ids or not isinstance(name, str) or not name.strip():
        return None
    average = (show.get("rating") or {}).get("average")
    ratings = {SOURCE: RatingReading(value=float(average))} if isinstance(average, (int, float)) and average > 0 else {}
    premiered = show.get("premiered")
    return Title(
        media_type=media_type_of(show), title=name.strip(), ids=ids, en_title=name.strip() if (show.get("language") or "") == "English" else None,
        alt_titles=None if akas is None else [{"name": a["name"]} for a in akas if isinstance(a, dict) and a.get("name")],
        year=_int(premiered[:4]) if isinstance(premiered, str) else None, poster=_image(show),
        overview=plain_text(show.get("summary")), genres=[g for g in show.get("genres") or [] if isinstance(g, str)],
        network=_network(show), runtime=_int(show.get("runtime")) or _int(show.get("averageRuntime")),
        status=_status(show), ratings=ratings,
        extra={"url": show["url"]} if isinstance(show.get("url"), str) else {})


def parse_episodes(raw: Any) -> List[Episode]:
    """Numbered episodes in airing order. Specials TVmaze does not number are left out."""
    out: List[Episode] = []
    for ep in raw if isinstance(raw, list) else []:
        if not isinstance(ep, dict):
            continue
        season, number = _int(ep.get("season")), _int(ep.get("number"))
        if season is None or number is None:
            continue
        out.append(Episode(season=season, number=number, title=ep.get("name") or None,
                           air_date=(ep.get("airdate") or None), ids=normal_ids({NAME: ep.get("id")})))
    out.sort(key=lambda e: (e.season, e.number))
    return out


def next_episode(show: Dict[str, Any]) -> Optional[ScheduledEpisode]:
    """The episode embedded as `nextepisode` in a show record, when it has a date."""
    ep = (show.get("_embedded") or {}).get("nextepisode")
    ids = show_ids(show)
    if not isinstance(ep, dict) or NAME not in ids:
        return None
    season, number, airdate = _int(ep.get("season")), _int(ep.get("number")), ep.get("airdate")
    if season is None or number is None or not airdate:
        return None
    return ScheduledEpisode(media_type=media_type_of(show), ids={NAME: ids[NAME]}, season=season, number=number,
                            air_date=airdate, title=ep.get("name") or None)
