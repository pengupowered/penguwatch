"""Data models for PenguWatch."""

from enum import Enum
from typing import Annotated, List, Optional, Dict, Any, Literal, Union
import re

from pydantic import BaseModel, BeforeValidator, Field, computed_field, field_validator, model_serializer, model_validator

from penguwatch.drivers.base import normal_ids
from penguwatch.ratings import TitleRating

_SERVICE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class ShowStatus(str, Enum):
    WATCHING = "watching"
    PLANTOWATCH = "plantowatch"
    HOLD = "hold"
    COMPLETED = "completed"
    DROPPED = "dropped"


class Priority(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# Who wants to watch a title: the ids of household members (config.HouseholdConfig: person1, person2, ...), at most
# MAX_MEMBERS of them. Empty on a new title means "the first member" (the repository fills it in).
Household = Annotated[List[Annotated[str, Field(pattern=r"^person[1-9][0-9]{0,5}$")]], Field(max_length=20)]


class MediaType(str, Enum):
    SHOW = "show"
    ANIME = "anime"
    MOVIE = "movie"


class ServiceConfig(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=80)
    monthly_cost: float = Field(default=0.0, ge=0, le=1000)
    is_active: bool = False
    renewal_day: Optional[int] = Field(default=None, ge=1, le=31)
    renewal_date: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    aliases: List[str] = Field(default_factory=list, max_length=40)
    # Exact titles this service carries whatever their network says (licensing exceptions), e.g. ["Seinfeld"].
    titles: List[str] = Field(default_factory=list, max_length=200)
    # Ids of the services a bundle includes; empty for a plain service.
    includes: List[str] = Field(default_factory=list, max_length=12)
    notes: Optional[str] = None
    # monthly_cost is always the monthly equivalent; for a yearly service renewal_date is a charge date.
    billing_period: Literal["monthly", "yearly"] = "monthly"

    @field_validator("aliases")
    @classmethod
    def _check_aliases(cls, value: List[str]) -> List[str]:
        if any(len(a) > 60 for a in value):
            raise ValueError("each alias must be at most 60 characters")
        return value

    @field_validator("titles")
    @classmethod
    def _check_titles(cls, value: List[str]) -> List[str]:
        if any(len(t) > 200 for t in value):
            raise ValueError("each title must be at most 200 characters")
        return value

    @field_validator("includes")
    @classmethod
    def _check_includes(cls, value: List[str]) -> List[str]:
        for sid in value:
            if not _SERVICE_ID_RE.match(sid):
                raise ValueError(f"invalid service id in includes: {sid!r}")
        if len(set(value)) != len(value):
            raise ValueError("includes must not contain duplicates")
        return value

    @model_validator(mode="after")
    def _check_not_self_including(self) -> "ServiceConfig":
        if self.id in self.includes:
            raise ValueError("a bundle cannot include itself")
        return self

    @property
    def is_bundle(self) -> bool:
        return bool(self.includes)


# A title's external ids, one per provider: {"imdb": "tt0903747", "tmdb": 1396, ...} (docs/api.md)
TitleIds = Dict[str, Union[int, str]]


def _ids(value: Any) -> TitleIds:
    return normal_ids(value if isinstance(value, dict) else {})


# TitleIds as models take them: provider names in lower case, numbers as integers, empty values left out
NormalIds = Annotated[TitleIds, BeforeValidator(_ids)]


class Show(BaseModel):
    id: Optional[int] = None
    ids: NormalIds = Field(default_factory=dict)
    media_type: MediaType = MediaType.SHOW
    title: str
    # English title from the catalogue when it differs from `title` (None otherwise); alt_titles = other known names.
    # Both are left out of python-mode dumps while empty (see _omit_unset_interest).
    en_title: Optional[str] = None
    alt_titles: List[str] = Field(default_factory=list)
    year: Optional[int] = None
    poster: Optional[str] = None
    banner: Optional[str] = None
    overview: Optional[str] = None
    genres: List[str] = Field(default_factory=list)
    network: Optional[str] = None
    service_id: Optional[str] = None
    # True once someone picks the service by hand: automatic matching never overrides it.
    service_manual: bool = False
    status: ShowStatus = ShowStatus.PLANTOWATCH
    priority: Priority = Priority.MEDIUM
    # The member ids who want to watch it, in the members' order (people no longer in the household left out)
    household: Household = Field(default_factory=list)
    simkl_rating: Optional[float] = None
    imdb_rating: Optional[float] = None
    imdb_votes: Optional[int] = None
    user_rating: Optional[float] = None
    total_episodes: int = 0
    watched_episodes: int = 0
    aired_episodes: int = 0
    runtime_minutes: int = 45
    next_episode_air_date: Optional[str] = None
    next_episode_season: Optional[int] = None
    next_episode_number: Optional[int] = None
    next_episode_title: Optional[str] = None
    is_ended: bool = False
    last_watched: Optional[str] = None
    next_to_watch: Optional[str] = None
    notes: Optional[str] = None
    last_watched_at: Optional[str] = None
    episodes_synced_at: Optional[str] = None
    enriched_at: Optional[str] = None
    # Anime only: direct sequels from the catalogue ([{ids, title, year, poster, anime_type}]); None = not checked yet
    sequels: Optional[List[Dict[str, Any]]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # Likely-interest score; only present (and serialized) when computed for a response.
    interest_score: Optional[int] = None
    interest_reasons: Optional[List[str]] = None
    # Values from custom rating sources, by source id (see ratings.py); the built-in sources stay in their own fields.
    ratings: Dict[str, TitleRating] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def _omit_unset_interest(self, handler, info):
        data = handler(self)
        if self.interest_score is None:
            data.pop("interest_score", None)
            data.pop("interest_reasons", None)
        if info.mode == "python":
            # In-process dicts (model_dump()) leave the English-title fields out while empty, so existing consumers
            # and fixtures see the shape they always did; the JSON API always carries them.
            if self.en_title is None:
                data.pop("en_title", None)
            if not self.alt_titles:
                data.pop("alt_titles", None)
            if not self.ratings:
                data.pop("ratings", None)
        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def episodes_left(self) -> int:
        if self.status in (ShowStatus.COMPLETED, ShowStatus.DROPPED):
            return 0
        return max(0, self.aired_episodes - self.watched_episodes)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_caught_up(self) -> bool:
        if self.status in (ShowStatus.COMPLETED, ShowStatus.DROPPED):
            return True
        return self.aired_episodes > 0 and self.watched_episodes >= self.aired_episodes

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_ready_to_binge(self) -> bool:
        # Completed or dropped titles do not need binging
        if self.status in (ShowStatus.COMPLETED, ShowStatus.DROPPED):
            return False
        if self.episodes_left <= 0:
            return False
        if self.media_type == MediaType.MOVIE or self.is_ended:
            return True
        # Unwatched episodes and nothing new scheduled: the current season is complete
        return self.next_episode_air_date is None


class Episode(BaseModel):
    id: Optional[int] = None
    show_id: int
    ids: NormalIds = Field(default_factory=dict)  # the provider's own ids of the episode, when known
    season: int
    number: int
    title: Optional[str] = None
    air_date: Optional[str] = None
    is_watched: bool = False
    watched_at: Optional[str] = None


class ShowCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    year: Optional[int] = None
    # The title's ids (from a catalogue search): {"imdb": "tt...", "tmdb": 1396, ...}. With an id the catalogue
    # knows, the title is looked up by it; without, by name.
    ids: NormalIds = Field(default_factory=dict)
    media_type: Optional[MediaType] = None
    network: Optional[str] = None
    service_id: Optional[str] = None
    status: ShowStatus = ShowStatus.PLANTOWATCH
    priority: Priority = Priority.MEDIUM
    household: Household = Field(default_factory=list)  # empty: the first member
    overview: Optional[str] = None
    poster: Optional[str] = None
    banner: Optional[str] = None
    genres: List[str] = Field(default_factory=list)
    imdb_rating: Optional[float] = None
    total_episodes: int = 0
    aired_episodes: int = 0
    watched_episodes: int = 0


class ShowUpdateRequest(BaseModel):
    status: Optional[ShowStatus] = None
    priority: Optional[Priority] = None
    household: Optional[Household] = None  # the whole set (at least one member); None leaves it as it is
    service_id: Optional[str] = None
    network: Optional[str] = None
    watched_episodes: Optional[int] = None
    user_rating: Optional[float] = None
    clear_user_rating: bool = False
    notes: Optional[str] = None


class OptimizerRecommendation(BaseModel):
    service_id: str
    service_name: str
    is_active: bool
    monthly_cost: float
    binge_score: float
    hours_backlog: float
    episodes_backlog: int
    shows_count: int
    ready_to_binge_count: int
    shared_count: int  # titles two or more people want
    avg_imdb_rating: Optional[float] = None
    top_shows: List[Dict[str, Any]] = Field(default_factory=list)
    recommendation_reason: str
    action: str  # "keep_active", "activate_now", "cancel_recommended", "wait_for_finale", "standby"
    # Value & pace (viewing history over the plan's value window)
    episodes_watched_window: int = 0
    hours_watched_window: float = 0.0
    cost_per_hour: Optional[float] = None  # active services only: monthly cost / hours watched
    pace_hours_per_day: Optional[float] = None
    days_to_clear: Optional[int] = None  # backlog hours / this service's own pace
    next_renewal: Optional[str] = None
    renewal_in_days: Optional[int] = None
    verdict: Optional[str] = None  # "unused", "finish_before_renewal", "long_tail", "in_use"
    verdict_text: Optional[str] = None
    # Bundles: the bundle that covers this service, and (for a bundle) its members
    covered_by: Optional[str] = None
    includes: List[str] = Field(default_factory=list)
    is_bundle: bool = False
    # Planning: weighted remaining hours (see optimizer/plannable.py) and price flags
    plannable_hours: float = 0.0
    is_free: bool = False  # configured price is 0 and it is not a bundle
    needs_price: bool = False  # a bundle with no price set
    top_show_scores: Dict[int, int] = Field(default_factory=dict)  # show id -> likely-interest score of top_shows
    # One status per service (optimizer/status.py); None on hand-built recommendations
    billing_period: str = "monthly"
    status: Optional[str] = None  # inactive|covered|included|paid_through|decide_soon|check|in_use|watching|consider_cancel
    status_text: str = ""
    decide_by: Optional[str] = None
    watching_titles: List[str] = Field(default_factory=list)
    savable_monthly_cost: float = 0.0


class BundleSuggestion(BaseModel):
    kind: Literal["switch_to_bundle", "bundle_not_worth_it"]
    bundle_id: str
    bundle_name: str
    member_ids: List[str]
    current_cost: float  # what is paid now for those members
    bundle_cost: float
    monthly_saving: float  # positive = money saved by following the suggestion
    text: str


class OptimizerPlan(BaseModel):
    current_monthly_spend: float
    all_services_spend: float
    monthly_savings: float
    annual_savings: float
    max_active_services: int
    active_services: List[ServiceConfig]
    service_rankings: List[OptimizerRecommendation]
    rotation_advice: List[str]
    value_window_days: int = 90
    unused_monthly_cost: float = 0.0  # active services with no viewing in the window
    household_hours_per_day: float = 0.0
    bundle_suggestions: List[BundleSuggestion] = Field(default_factory=list)
    unassigned_titles: int = 0  # open titles with no service assigned
    savable_monthly_cost: float = 0.0  # what cancelling the services marked consider_cancel or decide_soon saves
    next_decision: Optional[Dict[str, Any]] = None  # {service_id, service_name, date, days, kind}


class RotationSegment(BaseModel):
    start_month: int  # 0-based, inclusive
    end_month: int  # inclusive
    kind: Literal["keep", "subscribe", "cancel", "pause", "wait"]
    label: str


class RotationService(BaseModel):
    service_id: str
    name: str
    monthly_cost: float
    is_free: bool = False
    is_bundle: bool = False
    includes: List[str] = Field(default_factory=list)
    covered_by: Optional[str] = None
    segments: List[RotationSegment] = Field(default_factory=list)


class RotationPick(BaseModel):
    show_id: int
    title: str
    en_title: Optional[str] = None  # left out of the payload while unknown
    interest_score: Optional[int] = None

    @model_serializer(mode="wrap")
    def _omit_unknown_en_title(self, handler):
        data = handler(self)
        if self.en_title is None:
            data.pop("en_title", None)
        return data


class RotationSummary(BaseModel):
    current_monthly_spend: float
    planned_avg_monthly_spend: float
    monthly_difference: float  # planned - current (negative = cheaper)
    cancel_now: List[str]  # service ids


class RotationPlan(BaseModel):
    months: int
    slots: int
    pace_hours_per_day: float
    pace_floor_applied: bool
    unassigned_titles: int
    summary: RotationSummary
    services: List[RotationService]
    picks: Dict[str, List[RotationPick]]
    notes: List[str]

class RandomPickFilter(BaseModel):
    service_id: Optional[str] = None
    only_active_services: bool = True
    member: Optional[str] = Field(default=None, pattern=r"^person[1-9][0-9]{0,5}$")  # titles this person wants
    shared: Optional[bool] = None  # True: titles two or more people want; False: titles one person wants alone
    status: Optional[ShowStatus] = None
    media_type: Optional[MediaType] = None
    min_rating: Optional[float] = None
    genre: Optional[str] = None
    ready_to_binge_only: bool = False


class ShowProgressUpdate(BaseModel):
    # Bounded: the handler loops once per episode, and each step queues a change for the library
    watched_episodes: Optional[int] = Field(default=None, ge=0, le=5000)
    episode_delta: Optional[int] = Field(default=None, ge=-200, le=200)
    season_delta: Optional[int] = Field(default=None, ge=-30, le=30)


class EpisodeToggleRequest(BaseModel):
    season: int = Field(ge=0, le=500)
    number: int = Field(ge=0, le=5000)
    watched: bool = True


class SeasonToggleRequest(BaseModel):
    season: int = Field(ge=0, le=500)
    watched: bool = True
