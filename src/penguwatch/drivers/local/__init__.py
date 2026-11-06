"""The local library driver: no account, nothing to sync. The library, the watch history and the ratings live only in
PenguWatch. It offers only `library_sync`; titles still come from the catalogue drivers.

Its library has `remote = False` (drivers.base.LibrarySync): the sync engine never pushes or pulls, and treats review
mode as off. It is always connected, so nothing asks for a login. Switching to it from an account driver deletes no
title, watched episode or rating, and changes the account driver had not delivered yet stay queued for it. Edits made
while local to a title that driver knows (watched, status, rating, delete) are also kept for it as unapproved outbox
rows (the engine remembers the last account driver, `library.last_account`): never delivered while local; once the
account driver is the library again a pull leaves those titles alone until you approve the rows (they go out) or
decline them (the account's version comes back). A title without that driver's id, or added while local, gets no row.

Titles added while it is active get a `local` id (drivers.base.LOCAL_ID), so a later pull from an account's listing
knows they were never on it and does not remove them."""

from typing import Any, AsyncIterator, ClassVar, FrozenSet, Hashable, List, Mapping, Optional, Sequence, Tuple

from penguwatch.drivers.base import (
    LOCAL_ID, BuiltinRatingSource, Capability, ConnectStep, DriverStatus, LibraryEntry, PullBatch, PullPlan,
    SyncChange, SyncError, SyncOp,
)

NAME = "local"
LOCAL_ONLY_MESSAGE = "Local only: your library, watch history and ratings are kept in PenguWatch. Nothing is sent to an account."


class LocalLibrary:
    """library_sync without an account: every call that would reach one is a no-op."""

    name = NAME
    label = "Local"
    key_provider = LOCAL_ID
    remote = False
    # The worker's upkeep cadence (catalogue details, episode lists, the schedule); there is nothing to pull
    pull_interval_minutes = 30
    idle_pull_interval_minutes = 180
    idle_after_hours = 12

    def is_connected(self) -> bool:
        return True

    async def needs_reconnect(self) -> bool:
        return False

    async def maintain(self) -> Optional[str]:
        return None

    async def on_auth_error(self, error: SyncError) -> str:
        return error.message

    async def confirmed(self) -> None:
        return None

    async def status(self) -> DriverStatus:
        return DriverStatus(driver=NAME, connected=True, auth_state="ok", configured=True, message=LOCAL_ONLY_MESSAGE,
                            details={"local_only": True})

    async def connect(self, step: Optional[Mapping[str, Any]] = None) -> ConnectStep:
        return ConnectStep(kind="error", message="The local library has no account to connect.")

    async def disconnect(self) -> None:
        return None

    async def plan_pull(self, cursor: Any, full: bool) -> PullPlan:
        return PullPlan(mode="none", cursor=cursor)

    async def pull(self, plan: PullPlan, cursor: Any) -> AsyncIterator[PullBatch]:
        return
        yield  # pragma: no cover  (makes this an async generator)

    def decode_entry(self, media_type: str, native: Any) -> Optional[LibraryEntry]:
        return None

    async def entries(self, media_type: str) -> List[SyncChange]:
        return []

    def batch_key(self, op: SyncOp) -> Optional[Hashable]:
        return None

    async def push(self, ops: Sequence[SyncOp]) -> Any:
        raise SyncError("rejected", "The local library has nowhere to push to.", permanent=True)


class LocalService:
    """The running local driver: only a library."""

    name = NAME
    label = "Local"

    def __init__(self) -> None:
        self.library = LocalLibrary()
        self.catalog = None
        self.schedule = None
        self.discovery = None

    async def aclose(self) -> None:
        return None


class LocalDriver:
    name: ClassVar[str] = NAME
    label: ClassVar[str] = "Local"
    offers: ClassVar[FrozenSet[Capability]] = frozenset({Capability.LIBRARY_SYNC})
    options_model: ClassVar[None] = None
    rating_sources: ClassVar[Tuple[BuiltinRatingSource, ...]] = ()
    secret_state_keys: ClassVar[Tuple[str, ...]] = ()
    key_provider: ClassVar[str] = LOCAL_ID
    remote: ClassVar[bool] = False

    @classmethod
    async def build(cls, repository, options, *, app_version: str, on_request=None) -> LocalService:
        return LocalService()
