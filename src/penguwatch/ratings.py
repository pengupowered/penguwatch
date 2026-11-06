"""Rating sources: where a rating on a title comes from, and the pure rules around them.

Built-in sources come from the drivers' ratings capability (drivers.base.BuiltinRatingSource): their values live in
columns of `shows` and are read-only. A built-in source without a column (BuiltinRatingSource.column None) is stored
in `title_ratings` under a `rating_sources` row of origin 'builtin' (seeded for the drivers config.yaml runs): listed as
built-in and read-only, its driver fills it.
Custom sources are rows in `rating_sources` with their values in `title_ratings`; they come from config.yaml
(`ratings: sources:`) or from the API. A value is validated against the source's scale, and a source id is a short
lowercase slug that is safe in a URL, a CSV header and a query string.
"""

import json
import math
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from penguwatch.drivers import DRIVERS
from penguwatch.drivers.base import IdValue, normal_id
from penguwatch.textsearch import clean_alt_titles, normalize

SOURCE_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"
SOURCE_ID_RE = re.compile(SOURCE_ID_PATTERN)
MAX_BATCH = 1000
MAX_URL = 500



def _builtin_specs():
    """Every registered driver's built-in rating sources, the first driver's definition of an id winning."""
    seen: Dict[str, Any] = {}
    for driver in DRIVERS.values():
        for spec in driver.rating_sources:
            seen.setdefault(spec.id, spec)
    return list(seen.values())


def builtin_rating_columns() -> Dict[str, Tuple[str, Optional[str]]]:
    """built-in source id -> (its column in shows, its votes column or None), for the sources that have a column."""
    return {spec.id: (spec.column, spec.votes_column) for spec in _builtin_specs() if spec.column}


# built-in source id -> its column in shows
BUILTIN_COLUMNS: Dict[str, str] = {spec.id: spec.column for spec in _builtin_specs() if spec.column}
# Built-in sources without a column: stored in title_ratings like a custom source, but filled by their driver
STORED_BUILTIN_IDS = frozenset(spec.id for spec in _builtin_specs() if not spec.column)


def stored_builtin_specs(driver_names: Iterable[str]) -> List[Any]:
    """The column-less built-in rating sources of the named drivers (those the configuration runs)."""
    return [spec for name in dict.fromkeys(driver_names) if name in DRIVERS
            for spec in DRIVERS[name].rating_sources if not spec.column]


# Ids a custom source may not take: the built-ins, the user's own rating, and the MAL name of the IMDb column.
RESERVED_IDS = frozenset({*BUILTIN_COLUMNS, *STORED_BUILTIN_IDS, "mal", "user", "my", "your"})


def check_source_id(value: str) -> str:
    if not SOURCE_ID_RE.fullmatch(value or ""):
        raise ValueError("a source id is 1 to 32 characters: lowercase letters, digits, '-' and '_', starting with a letter or digit")
    return value


def _clean_text(value: str, what: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{what} must not be empty")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{what} must not contain control characters")
    return value


class RatingScale(BaseModel):
    scale_min: float = Field(default=0.0, ge=-1_000_000, le=1_000_000)
    scale_max: float = Field(default=10.0, ge=-1_000_000, le=1_000_000)
    decimals: int = Field(default=1, ge=0, le=3)

    @model_validator(mode="after")
    def _ordered(self):
        if not self.scale_min < self.scale_max:
            raise ValueError("scale_min must be less than scale_max")
        return self


class RatingSourceSpec(RatingScale):
    """A custom rating source as written in config.yaml or sent to the API."""

    id: str
    name: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=8, description="Short label for badges, e.g. LB")
    show_on_cards: bool = False

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        check_source_id(value)
        if value in RESERVED_IDS:
            raise ValueError(f"'{value}' is reserved for a built-in rating")
        return value

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _clean_text(value, "name")

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        return _clean_text(value, "label")


class RatingSourcePatch(BaseModel):
    """Fields of a custom source that may be changed; omitted ones stay."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=60)
    label: Optional[str] = Field(default=None, min_length=1, max_length=8)
    scale_min: Optional[float] = Field(default=None, ge=-1_000_000, le=1_000_000)
    scale_max: Optional[float] = Field(default=None, ge=-1_000_000, le=1_000_000)
    decimals: Optional[int] = Field(default=None, ge=0, le=3)
    show_on_cards: Optional[bool] = None
    position: Optional[int] = Field(default=None, ge=0, le=10_000)

    @field_validator("name")
    @classmethod
    def _name(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _clean_text(value, "name")

    @field_validator("label")
    @classmethod
    def _label(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _clean_text(value, "label")


class RatingSource(BaseModel):
    """A source as the API lists it."""

    id: str
    name: str
    label: str
    scale_min: float
    scale_max: float
    decimals: int
    show_on_cards: bool
    position: int
    builtin: bool = False
    origin: Literal["builtin", "config", "api"] = "api"
    read_only: bool = False  # built-in, or defined in config.yaml (change it there)
    rated_titles: int = 0


BUILTIN_SOURCES: List[RatingSource] = [
    RatingSource(id=spec.id, name=spec.name, label=spec.label, scale_min=spec.scale_min, scale_max=spec.scale_max,
                 decimals=spec.decimals, show_on_cards=spec.show_on_cards, position=i, builtin=True, origin="builtin",
                 read_only=True)
    for i, spec in enumerate(sp for sp in _builtin_specs() if sp.column)
]


_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# Formats checked for the providers that have a well-known one; any other provider's id is a number or a short text
ID_FORMATS = {"imdb": re.compile(r"^tt\d{1,12}$")}


class RatingMatch(BaseModel):
    """How an imported value finds its title: exactly one external id, named by its provider as in a title's `ids`
    ({"imdb": "tt0903747"}, {"tmdb": 1396}, ...), or a title (with the year when known)."""

    model_config = ConfigDict(extra="allow", json_schema_extra={
        "description": "One external id under its provider's name (imdb, tmdb, tvdb, or any provider in a title's "
                       "ids), or title with an optional year."})

    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    year: Optional[int] = Field(default=None, ge=1880, le=2200)
    media_type: Optional[Literal["show", "anime", "movie"]] = None

    @model_validator(mode="after")
    def _one_way(self):
        ids = self.ids
        if len(ids) + (1 if self.title else 0) != 1:
            raise ValueError("give exactly one external id (imdb, tmdb, tvdb, ...) or title")
        if self.year is not None and not self.title:
            raise ValueError("year goes with title")
        return self

    @property
    def ids(self) -> Dict[str, IdValue]:
        """The external ids given (validated): {provider: id}."""
        out: Dict[str, IdValue] = {}
        for provider, raw in (self.model_extra or {}).items():
            if not _PROVIDER_RE.fullmatch(provider):
                raise ValueError(f"{provider!r} is not a field or a provider name")
            value = normal_id(raw)
            fmt = ID_FORMATS.get(provider)
            if value is None or (fmt is not None and not fmt.fullmatch(str(value))):
                raise ValueError(f"{provider}: not a valid id")
            out[provider] = value
        return out


def safe_url(value: Optional[str]) -> Optional[str]:
    """A link shown in the page must be http(s): `javascript:` and `data:` links are refused."""
    if value is None or value == "":
        return None
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc or len(value) > MAX_URL:
        raise ValueError(f"url must be an http(s) address of at most {MAX_URL} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("url must not contain control characters")
    return value


class RatingValue(BaseModel):
    """One value for one title; used as the body of a single PUT and inside an import item."""

    value: float
    votes: Optional[int] = Field(default=None, ge=0, le=2_000_000_000)
    url: Optional[str] = None

    @field_validator("value")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("value must be a finite number")
        return value

    @field_validator("url")
    @classmethod
    def _url(cls, value: Optional[str]) -> Optional[str]:
        return safe_url(value)


class RatingImportItem(RatingValue):
    match: RatingMatch


class RatingImportRequest(BaseModel):
    items: List[RatingImportItem] = Field(min_length=1, max_length=MAX_BATCH)


class TitleRating(BaseModel):
    """A stored value as it appears on a title."""

    value: float
    votes: Optional[int] = None
    url: Optional[str] = None
    updated_at: Optional[str] = None


def value_in_scale(source: RatingScale, value: float) -> bool:
    return source.scale_min <= value <= source.scale_max


def scale_error(source: RatingSource, value: float) -> Optional[str]:
    if value_in_scale(source, value):
        return None
    return f"{value:g} is outside {source.name}'s scale ({source.scale_min:g} to {source.scale_max:g})"


def parse_min_rating(text: str) -> tuple:
    """'letterboxd:3.5' -> ('letterboxd', 3.5). Raises ValueError."""
    source, sep, number = (text or "").partition(":")
    if not sep:
        raise ValueError("min_rating is <source>:<value>, for example letterboxd:3.5")
    check_source_id(source)
    try:
        value = float(number)
    except ValueError:
        raise ValueError("min_rating needs a number after the colon") from None
    if not math.isfinite(value):
        raise ValueError("min_rating needs a finite number")
    return source, value


def csv_column(source_id: str) -> str:
    return f"rating_{source_id}"


class TitleIndex:
    """The library's titles by catalogue id and by normalized name, to find which title an imported value is for.

    Names are compared the forgiving way (case, accents and punctuation ignored, see textsearch.normalize) against
    the title, the English title and the alternative titles."""

    def __init__(self, rows: Iterable[Any]):
        """`rows` have id, title, en_title, alt_titles_json, year, media_type and ids ({provider: id})."""
        self._by_id: Dict[Tuple[str, Any], List[Any]] = defaultdict(list)
        self._by_name: Dict[str, List[Any]] = defaultdict(list)
        for row in rows:
            for provider, value in (row["ids"] or {}).items():
                self._by_id[(provider, value)].append(row)
            names = [row["title"], row["en_title"]]
            try:
                names += clean_alt_titles(json.loads(row["alt_titles_json"] or "[]"))
            except ValueError:
                pass
            seen = set()
            for name in names:
                key = normalize(name)
                if key and key not in seen:
                    seen.add(key)
                    self._by_name[key].append(row)

    def find(self, match: RatingMatch) -> List[Any]:
        """The titles that match, without repeats. One means matched, none unmatched, several ambiguous."""
        if match.title:
            found = list(self._by_name.get(normalize(match.title), ()))
            if match.year is not None:
                found = [r for r in found if r["year"] == match.year]
        else:
            found = list(self._by_id.get(next(iter(match.ids.items())), ()))
        if match.media_type:
            found = [r for r in found if (r["media_type"] or "show") == match.media_type]
        return list({r["id"]: r for r in found}.values())
