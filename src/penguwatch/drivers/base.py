"""What a driver gives PenguWatch, independent of the service behind it.

A *driver* talks to one service (a tracker with an account, a public catalogue) and turns its answers into the
neutral types below. Everything else (the library, the sync engine, the API) works on these types and on
`Capability` names, never on a service: a new service is a new driver, and nothing else changes.

The capabilities, one small interface each:

- `catalog` (`CatalogProvider`): search, a title's details, its episode list, related titles (sequels).
- `library_sync` (`LibrarySync`): an account's library. A connect flow the page renders generically
  (`ConnectStep`), a pull that yields neutral changes, a push of neutral operations, the login status. One library
  driver is active at a time: it is the source of truth the sync engine keeps the local library in step with. A
  library driver with `remote = False` has no account: the library lives only in PenguWatch, and the engine neither
  pushes nor pulls for it (it only keeps edits for the last account driver, unapproved: `PushMixin._queue_for_account`).
- `schedule` (`ScheduleProvider`): upcoming and airing episodes.
- `discovery` (`DiscoveryProvider`): trending titles and "because you liked" recommendations.
- `ratings` (`RatingProvider`): built-in rating sources; their values travel on `Title.ratings`.

Titles are identified by a set of external ids, `Ids` ({"imdb": "tt0903747", "tmdb": 1396, ...}: one per
provider), episodes by season and number. A value a service did not report is None (or empty), never guessed.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any, AsyncIterator, ClassVar, Dict, FrozenSet, Hashable, List, Mapping, Optional, Protocol, Sequence, Tuple, Union,
)


class Capability(str, Enum):
    CATALOG = "catalog"
    LIBRARY_SYNC = "library_sync"
    SCHEDULE = "schedule"
    DISCOVERY = "discovery"
    RATINGS = "ratings"


# The roles config.yaml's `drivers:` assigns, and the capability a driver needs for each. `library` names one driver;
# the others a list, tried in order (the first that has the title wins).
ROLES: Dict[str, Capability] = {
    "library": Capability.LIBRARY_SYNC,
    "catalog": Capability.CATALOG,
    "schedule": Capability.SCHEDULE,
    "discovery": Capability.DISCOVERY,
}

MEDIA_TYPES = ("show", "anime", "movie")  # penguwatch.models.MediaType values

IdValue = Union[int, str]
Ids = Dict[str, IdValue]


def normal_id(value: Any) -> Optional[IdValue]:
    """An external id as stored: an int for a number (also when it came as a string of digits), else the trimmed
    string; None for nothing usable (None, "", 0, a bool)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit() and not (len(text) > 1 and text.startswith("0")):
            return int(text) or None
        return text[:200]
    return None


def normal_ids(raw: Optional[Mapping[str, Any]]) -> Ids:
    """`raw` with lower-case provider names and normal_id values; entries without a usable value are left out."""
    out: Ids = {}
    for key, value in (raw or {}).items():
        if not isinstance(key, str) or not key.strip():
            continue
        v = normal_id(value)
        if v is not None:
            out[key.strip().lower()[:32]] = v
    return out


# The provider name of the id a title gets when it is added while the library driver keeps no account (`remote` is
# False): it marks a title that was never on any account, so a later pull from an account's listing never removes it.
LOCAL_ID = "local"


def state_key(driver: str, key: str) -> str:
    """A driver's app_state key: its own namespace, `driver.<name>.<key>`."""
    return f"driver.{driver}.{key}"


# ---------------------------------------------------------------------------------------------- neutral data types


@dataclass(frozen=True)
class RatingReading:
    """One built-in rating source's value for a title (`votes` when the service says). A `fallback` reading is a
    stand-in (an anime's MyAnimeList score in the IMDb source, say): it only fills a title that has no value yet."""

    value: Optional[float]
    votes: Optional[int] = None
    fallback: bool = False


@dataclass
class Episode:
    """One episode of a title, by season and number (season 0 is specials). `ids` are the provider's own ids for the
    episode, when it has them."""

    season: int
    number: int
    title: Optional[str] = None
    air_date: Optional[str] = None  # YYYY-MM-DD
    ids: Ids = field(default_factory=dict)


@dataclass
class RelatedTitle:
    """A title related to another one (today: an anime's direct sequel)."""

    ids: Ids
    title: Optional[str] = None
    year: Optional[int] = None
    poster: Optional[str] = None
    kind: Optional[str] = None  # the provider's word for what it is (tv, movie, ova, ...)


@dataclass
class Title:
    """What a catalogue knows about a title. Unknown values stay None (or empty); `ratings` maps built-in rating
    source ids (RatingProvider.rating_sources) to their value."""

    media_type: str
    title: str
    ids: Ids = field(default_factory=dict)
    en_title: Optional[str] = None  # the English title as the catalogue gives it (may only restate `title`)
    alt_titles: Optional[List[Any]] = None  # other names, as the catalogue gives them; None: not asked
    year: Optional[int] = None
    poster: Optional[str] = None
    banner: Optional[str] = None
    overview: Optional[str] = None
    genres: List[str] = field(default_factory=list)
    network: Optional[str] = None
    runtime: Optional[int] = None
    status: Optional[str] = None  # the service's word: "ended", "airing", ...
    total_episodes: Optional[int] = None
    ratings: Dict[str, RatingReading] = field(default_factory=dict)
    related: Optional[List[RelatedTitle]] = None  # None: not asked
    extra: Dict[str, Any] = field(default_factory=dict)  # what one driver adds for its own use (never read elsewhere)


@dataclass
class ScheduledEpisode:
    """An episode the schedule says airs on `air_date`, for the title with `ids`."""

    media_type: Optional[str]
    ids: Ids
    season: int
    number: int
    air_date: str  # YYYY-MM-DD
    title: Optional[str] = None


@dataclass
class LibraryEntry:
    """One title as it stands in an account's library: its list status and, when the service gave them, the user's
    rating, the watched episodes and the progress counters.

    `watched` is None when the answer did not list episodes (keep the local ones); `has_rating` says whether the
    answer said anything about the rating (None may then mean "no rating")."""

    title: Title
    status: str
    has_rating: bool = False
    user_rating: Optional[float] = None
    watched: Optional[List[Tuple[int, int, Optional[str]]]] = None  # (season, number, watched_at)
    watched_count: int = 0
    total_episodes: int = 0
    aired_episodes: int = 0
    last_watched: Optional[str] = None
    next_to_watch: Optional[str] = None
    last_watched_at: Optional[str] = None
    is_ended: bool = False
    runtime: Optional[int] = None


@dataclass
class SyncChange:
    """One title a pull found. kind "entry": `entry` is the title as the library has it now; "rating": only its
    rating changed (`rating`; `entry` carries the title for when it is not known here yet). `native` is the
    service's own record, kept for review mode and given back to LibrarySync.decode_entry when the user accepts."""

    kind: str
    media_type: str
    ids: Ids
    entry: Optional[LibraryEntry] = None
    rating: Optional[float] = None
    native: Any = None


@dataclass
class PullBatch:
    """Part of a pull, applied before the next part is fetched: first `changes`, then, when `listing` is set,
    `remote_keys` are every title of `media_type` the library has (their key_provider ids): local titles of that
    type that are missing there (and not waiting to be pushed) were removed."""

    media_type: str
    changes: List[SyncChange] = field(default_factory=list)
    listing: bool = False
    remote_keys: FrozenSet[Hashable] = frozenset()


@dataclass
class PullPlan:
    """What a pull will do. mode "none": nothing changed since the stored cursor; "full" or "delta". `cursor` (any
    JSON value) is the new position, stored by the engine only once everything the pull yields has been applied (or
    staged); `detail` is the driver's own plan."""

    mode: str
    cursor: Any
    detail: Any = None


# The local operations a library driver pushes (SyncOp.kind)
OP_WATCHED = "watched"          # episodes (or a movie) watched, with watched_at
OP_UNWATCHED = "unwatched"      # episodes (or a movie) no longer watched
OP_STATUS = "status"            # the title's list status (also adds it to the library)
OP_RATE = "rate"                # the user's rating, 1-10
OP_UNRATE = "unrate"            # the user's rating removed
OP_REMOVE = "remove"            # the title removed from the library (lists and history)
OP_KINDS = (OP_WATCHED, OP_UNWATCHED, OP_STATUS, OP_RATE, OP_UNRATE, OP_REMOVE)


@dataclass
class SyncOp:
    """One queued local change for the library driver (an outbox row). `ids` are the title's ids when the change was
    made; `episodes` (season, number) pairs (empty for a movie); `watched_at` for OP_WATCHED; `status` for OP_STATUS;
    `rating` for OP_RATE. `native` is a payload in the driver's own format, sent as it is (rows queued before the
    driver layer existed)."""

    kind: str
    media_type: str
    ids: Ids
    episodes: List[Tuple[int, int]] = field(default_factory=list)
    watched_at: Optional[str] = None
    status: Optional[str] = None
    rating: Optional[int] = None
    native: Optional[Dict[str, Any]] = None
    row_id: Optional[int] = None  # the outbox row
    # Held until a full pull has shown what the account has (sync.pull). "add": the add of a title created while the
    # account was not connected ("add if missing": it never overwrites the account); "edit": a later change of that
    # title. None: not held.
    if_missing: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"kind": self.kind, "media_type": self.media_type, "ids": dict(self.ids)}
        if self.episodes:
            data["episodes"] = [[s, n] for s, n in self.episodes]
        for key in ("watched_at", "status", "rating", "native"):
            if getattr(self, key) is not None:
                data[key] = getattr(self, key)
        if self.if_missing:
            data["if_missing"] = self.if_missing
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any], row_id: Optional[int] = None) -> "SyncOp":
        return cls(
            kind=str(data.get("kind") or ""), media_type=str(data.get("media_type") or "show"),
            ids=normal_ids(data.get("ids")),
            episodes=[(int(s), int(n)) for s, n in data.get("episodes") or []],
            watched_at=data.get("watched_at"), status=data.get("status"), rating=data.get("rating"),
            native=data.get("native"), row_id=row_id, if_missing=(data.get("if_missing") if data.get("if_missing") in ("add", "edit")
                        else ("add" if data.get("if_missing") else None)),  # a plain true (cdce95b) is an add
        )


class SyncError(Exception):
    """A pull or push the service did not accept. `kind`:

    - "auth": the login no longer works (the run stops; LibrarySync.on_auth_error records it and says what to show)
    - "scope": the login may read but not write (the run stops; `message` says what to do)
    - "duplicate": already recorded there (the op is done)
    - "transient": try again later (rate limit, server error, network)
    - "rejected": the service refused it; `permanent` when trying again cannot help (dropped after a few tries)
    """

    def __init__(self, kind: str, message: str, *, permanent: bool = False):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.permanent = permanent


@dataclass
class ConnectStep:
    """One step of connecting an account, rendered by the page without knowing the service. kind:

    - "code": show `code`, open `url`, then call the next step with `state` every `interval` seconds
      (until `expires_in`)
    - "pending": not approved yet; poll again with the same state
    - "done": connected
    - "error": `message` says why
    """

    kind: str
    code: Optional[str] = None
    url: Optional[str] = None
    interval: Optional[int] = None
    expires_in: Optional[int] = None
    state: Dict[str, Any] = field(default_factory=dict)
    message: Optional[str] = None


@dataclass
class DriverStatus:
    """The login and sync state of a library driver. `auth_state`: disconnected, ok, expiring or needs_reconnect.
    `details` are the driver's own fields (shown by the page; the generic parts never read them)."""

    driver: str
    connected: bool
    auth_state: str
    configured: bool = True  # False: something the driver needs (an app id, ...) is not set
    message: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Attribution:
    """The credit a service's terms ask for wherever its data is shown: `text` (e.g. "Data from TVmaze") linked to
    `url`. A driver's `attribution` class attribute; the API lists it so the page can render it."""

    text: str
    url: str


@dataclass(frozen=True)
class BuiltinRatingSource:
    """A rating source a driver fills (RatingProvider). `column` is the storage column of `shows` that holds it; a
    source without one (None) is stored in `title_ratings` like a custom source, but is read-only (the driver fills
    it) and is listed as built-in."""

    id: str
    name: str
    label: str
    column: Optional[str] = None
    votes_column: Optional[str] = None
    scale_min: float = 0
    scale_max: float = 10
    decimals: int = 1
    show_on_cards: bool = True


# ---------------------------------------------------------------------------------------------- the interfaces


class CatalogProvider(Protocol):
    """Titles and episodes. Every lookup takes the title's ids; a catalogue looks titles up by its own id
    (`key_provider`), so the chain asks the first catalogue whose id the title has."""

    key_provider: str

    async def search(self, query: str, media_types: Sequence[str], limit: int = 10) -> List[Title]: ...

    async def title(self, media_type: Optional[str], ids: Ids) -> Optional[Title]:
        """The title's details (media_type None: any type), or None when not found."""

    async def episodes(self, media_type: str, ids: Ids) -> Optional[List[Episode]]:
        """The episode list in airing order; None when it could not be fetched (an empty list: none known)."""

    async def related(self, media_type: str, ids: Ids) -> Optional[List[RelatedTitle]]:
        """Direct sequels; None when it could not be fetched."""

    async def resolve(self, media_type: str, ids: Ids) -> Optional[Tuple[Ids, str]]:
        """(ids including this catalogue's own, media type) for a title known by other ids; None when not found."""


class LibrarySync(Protocol):
    """An account's library, kept in step by the sync engine (sync/). The engine owns the outbox, review mode,
    back-off, the cursor bookkeeping and applying changes; the driver talks to the service and keeps its login.

    A title belongs to the library through its id of `key_provider` (the driver's own id): only titles with one are
    pushed, and that id is the key a pulled title is matched by.

    `remote` says whether there is an account behind the library. True (an account): the engine queues every local
    change in the outbox, delivers it with `push` and keeps the library in step with `pull`. False (the library lives
    only in PenguWatch): the engine never calls `push`, `plan_pull` or `pull`, treats review mode as off, writes outbox
    rows only for the last account driver (unapproved, never delivered while local), and the worker only does the catalogue upkeep (details, episode lists, the schedule). The flag is explicit
    on purpose: a driver is not "local" because its `key_provider` happens never to match a title."""

    name: str
    label: str
    key_provider: str
    remote: bool
    # Pull intervals (minutes) the worker uses: while the app is in use, and after `idle_after_hours` without use
    pull_interval_minutes: int
    idle_pull_interval_minutes: int
    idle_after_hours: int

    def is_connected(self) -> bool: ...

    async def needs_reconnect(self) -> bool:
        """True while the login does not work and cannot renew itself (only a person can fix it)."""

    async def maintain(self) -> Optional[str]:
        """Housekeeping the worker runs before each step (renewing a login ahead of expiry); never raises. Returns
        "renewed" when it renewed the login."""

    async def on_auth_error(self, error: "SyncError") -> str:
        """Records that the service refused the login; returns the message to show."""

    async def confirmed(self) -> None:
        """An exchange with the account just succeeded (called once per delivery run): settles a reconnect banner."""

    async def status(self) -> DriverStatus: ...

    # Optionally `async def local_status(self) -> DriverStatus`: status() from stored and in-memory state only, with no
    # call to the service (status() may fetch account details). Readers that must not touch the account use it when
    # the driver has it (penguwatch.sync.summary).

    async def connect(self, step: Optional[Mapping[str, Any]] = None) -> ConnectStep:
        """Starts connecting (step None) or continues with the state the previous step returned."""

    async def disconnect(self) -> None: ...

    async def plan_pull(self, cursor: Any, full: bool) -> PullPlan:
        """Decides what to pull since `cursor` (a full pull when None or `full`). Raises SyncError."""

    def pull(self, plan: PullPlan, cursor: Any) -> AsyncIterator[PullBatch]:
        """The changes, a part at a time (each part is applied before the next is fetched). Raises SyncError."""

    def decode_entry(self, media_type: str, native: Any) -> Optional[LibraryEntry]:
        """The entry a SyncChange.native record stands for (review mode keeps the record until a person decides)."""

    async def entries(self, media_type: str) -> List[SyncChange]:
        """Every title of one type in the library now (kind "entry")."""

    def batch_key(self, op: SyncOp) -> Optional[Hashable]:
        """Ops of different titles with equal keys may go in one push; None: always alone."""

    async def push(self, ops: Sequence[SyncOp]) -> Any:
        """Delivers the ops (one, or several sharing a batch_key) in one request; raises SyncError."""


class ScheduleProvider(Protocol):
    async def upcoming(self) -> List[ScheduledEpisode]:
        """Episodes airing soon (the provider decides how far ahead); the engine keeps those of library titles."""


class DiscoveryProvider(Protocol):
    key_provider: str  # the id a seed title needs for recommendations

    async def trending(self, media_type: str, interval: str) -> List[Title]:
        """interval: today, week or month. Title.extra may carry "rank" and "watchers"."""

    async def recommendations(self, media_type: str, ids: Ids) -> List[Tuple[Title, float]]:
        """Titles people who liked the seed liked, best first, each with a score (higher is a stronger match)."""


class RatingProvider(Protocol):
    rating_sources: ClassVar[Tuple[BuiltinRatingSource, ...]]


@dataclass(frozen=True)
class DriverSetting:
    """One option of a driver's config.yaml section as the Settings tab shows it (penguwatch.settings.registry collects
    them from every driver's `settings`): `field` of its options_model, keyed "<driver>.<field>". `section` is a
    registry section id ("sync", "advanced"); its group is the driver's label. Type, range and default come from the
    field."""

    field: str
    section: str
    label: str
    effect: str
    unit: Optional[str] = None
    live: bool = True  # False: applies at the next start (the driver is built once)


class Driver(Protocol):
    """One service. A class with these attributes; `build(context)` makes the running instance, whose capability
    objects (`catalog`, `library`, `schedule`, `discovery`) are None for what it does not offer."""

    name: ClassVar[str]
    label: ClassVar[str]  # what the page calls it
    offers: ClassVar[FrozenSet[Capability]]
    options_model: ClassVar[Optional[type]]  # pydantic model of its config.yaml section (named after the driver)
    rating_sources: ClassVar[Tuple[BuiltinRatingSource, ...]]
    secret_state_keys: ClassVar[Tuple[str, ...]]  # its app_state keys that hold credentials (left out of backups)

    # What the page needs to show a driver without knowing it (all optional; GET /api/drivers reports them):
    # key_provider: ClassVar[str]       the id its titles are known by (default: the driver's name)
    # connect_kind: ClassVar[str]       how an account is connected ("device_code"); absent: nothing to connect
    # extras: ClassVar[Tuple[str, ...]] extra controls it offers: "credentials" (an app id form), "diagnose"
    # title_urls: ClassVar[Dict[str, str]]  media type -> a title's page on the service, "{id}" for its id
    # home_url: ClassVar[str]           the service's home page: credited where its data is used
    # The Settings tab (penguwatch.settings.registry; a driver with an options_model must place every option):
    # settings: ClassVar[Tuple[DriverSetting, ...]]   the options the page shows
    # not_on_page: ClassVar[Mapping[str, str]]       option -> why the page leaves it off (a credential)
    # legacy_defaults: ClassVar[Mapping[str, Any]]   option -> its default before the settings store (frozen, for the
    #                                                one-time import: penguwatch.settings.legacy)
