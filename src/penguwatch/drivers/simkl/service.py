"""The running SIMKL driver: the client with the stored login, and one object per capability.

- SimklLibrary (library_sync): the device-PIN login and its renewal, SIMKL's two-phase delta pull
  (https://api.simkl.org/guides/sync: GET /sync/activities, compared with the stored snapshot, then only the buckets
  that moved, with ?date_from=), and pushes of neutral operations (payloads.py).
- SimklCatalog (catalog): search, details, episode lists, sequels, and resolving a TMDB id.
- SimklSchedule (schedule): the airing lists and the user's calendar.
- SimklDiscovery (discovery): trending lists and "users also watched" / "similar" recommendations.
"""

import logging
import re
import time
from typing import Any, AsyncIterator, Dict, Hashable, List, Mapping, Optional, Sequence, Tuple

from penguwatch.drivers.base import (
    ConnectStep, DriverStatus, Episode, LibraryEntry, PullBatch, PullPlan, RelatedTitle, ScheduledEpisode, SyncChange,
    SyncError, SyncOp, Title, normal_ids, state_key,
)
from penguwatch.drivers.simkl import auth, mapper, payloads
from penguwatch.drivers.simkl.client import (
    DEFAULT_TTL, RENEW_MARGIN, SEARCH_LIMIT, SimklAuthError, SimklClient, SimklError, SimklScopeError, extract_simkl_id,
)
from penguwatch.drivers.simkl.mapper import DETAIL_MEDIA, MEDIA_OF, READ_ONLY_MSG, STATUS_BUCKETS, SYNC_TYPE_INFO
from penguwatch.models import MediaType
from penguwatch.textsearch import rank_matches, title_names

logger = logging.getLogger("penguwatch.drivers.simkl")

NAME = "simkl"
KEY_SCOPE = state_key(NAME, "scope")
KEY_CLIENT_ID = state_key(NAME, "client_id")
KEY_ACCESS = state_key(NAME, "access_token")
KEY_REFRESH = state_key(NAME, "refresh_token")
KEY_CURSOR = state_key(NAME, "cursor")
KEY_LAST_ERROR = state_key(NAME, "last_error")
SEARCH_KINDS = {"show": "tv", "anime": "anime", "movie": "movie"}
KIND_MEDIA = {"tv": "show", "anime": "anime", "movie": "movie"}


def _is_transient(e: Exception) -> bool:
    status = getattr(e, "status_code", 0)
    return status == 0 or status == 429 or status >= 500 or (status == 400 and "rate_limit" in str(e))


def _not_found_items(not_found: Any) -> List[Any]:
    """The items of SIMKL's `not_found` answer ({"shows": [...], "movies": [...], ...}, or a plain list)."""
    if isinstance(not_found, dict):
        return [item for v in not_found.values() if isinstance(v, list) for item in v]
    return list(not_found) if isinstance(not_found, list) else []


def _simkl_ids(not_found: Any) -> List[Any]:
    """The SIMKL ids among the items SIMKL did not find (never their titles)."""
    ids = []
    for item in _not_found_items(not_found):
        sid = (item.get("ids") or {}).get("simkl") if isinstance(item, dict) else None
        if isinstance(sid, int) or (isinstance(sid, str) and sid.isdigit()):
            ids.append(int(sid))
    return ids


def _auth_message(e: SimklAuthError) -> str:
    why = "expired" if "expired" in e.message else "was revoked or is invalid"
    return f"Your SIMKL login {why}. Click 'Reconnect SIMKL' to log in again."


class SimklService:
    """One SIMKL account: the client and the capability objects that share it."""

    name = NAME
    label = "SIMKL"

    def __init__(self, client: SimklClient, repository, options):
        self.client = client
        self.repository = repository
        self.options = options
        self.user_cache: Tuple[Any, Any] = (None, None)
        self.library = SimklLibrary(self)
        self.catalog = SimklCatalog(self)
        self.schedule = SimklSchedule(self)
        self.discovery = SimklDiscovery(self)

    @classmethod
    async def create(cls, repository, options, *, app_version: str, on_request=None) -> "SimklService":
        """The client with the stored login (else the configured one) and the callbacks that keep a renewal stored."""
        client_id = await repository.get_state(KEY_CLIENT_ID) or options.client_id
        access_token = await repository.get_state(KEY_ACCESS) or options.access_token
        client = SimklClient(client_id=client_id, access_token=access_token, app_name=options.app_name,
                             app_version=app_version)
        client.set_tokens(access_token, await repository.get_state(KEY_REFRESH), await auth.load_expiry(repository))

        async def persist(access: str, refresh: Optional[str], expires_at: Optional[float]):
            await auth.persist_tokens(repository, access, refresh, expires_at)

        async def rejected(expires_at: Optional[float], still_current=None):
            await auth.mark_renewal_failed(repository, expires_at, still_current)

        client.on_token_refreshed = persist
        client.on_auth_rejected = rejected
        client.on_request = on_request
        return cls(client, repository, options)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def ensure_credentials(self) -> None:
        """Picks up an app id or token stored after the start (the login steps write them)."""
        client, repo = self.client, self.repository
        if not client.client_id:
            cid = await repo.get_state(KEY_CLIENT_ID)
            if cid:
                client.client_id = cid
        if not client.access_token:
            tok = await repo.get_state(KEY_ACCESS)
            if tok:
                client.access_token = tok

    async def _call(self, coro):
        """Runs a SIMKL call of a pull, turning the login errors into SyncError."""
        try:
            return await coro
        except SimklScopeError as e:
            raise SyncError("scope", READ_ONLY_MSG) from e
        except SimklAuthError as e:
            raise SyncError("auth", _auth_message(e)) from e


class SimklLibrary:
    """library_sync: the account's watchlist, history and ratings."""

    name = NAME
    label = "SIMKL"
    key_provider = NAME
    remote = True

    def __init__(self, service: SimklService):
        self.service = service
        options = service.options
        self.pull_interval_minutes = options.auto_sync_interval_minutes
        self.idle_pull_interval_minutes = options.idle_sync_interval_minutes
        self.idle_after_hours = options.idle_after_hours

    @property
    def client(self) -> SimklClient:
        return self.service.client

    @property
    def repository(self):
        return self.service.repository

    # --- login ----------------------------------------------------------------------------------------------------

    def is_connected(self) -> bool:
        return bool(self.client.access_token)

    async def needs_reconnect(self) -> bool:
        """The stored state says a reconnect is needed AND the login cannot renew itself: the refresh token was
        refused, or there never was one (a pasted token)."""
        if await self.repository.get_state(auth.KEY_STATE) != auth.NEEDS_RECONNECT:
            return False
        return not (self.client.refresh_token and not self.client.refresh_rejected)

    async def maintain(self) -> Optional[str]:
        return await self.client.renew_if_due()

    async def on_auth_error(self, error: SyncError) -> str:
        return await auth.record_auth_failure(self.repository, self.client, error.message)

    async def confirmed(self) -> None:
        await auth.token_works(self.repository, self.client)

    async def status(self) -> DriverStatus:
        await self.service.ensure_credentials()
        client, repo = self.client, self.repository
        login = await auth.describe(repo, client, RENEW_MARGIN)
        user_info = None
        if client.access_token:
            user_info, self.service.user_cache = await auth.cached_user_info(self.service.user_cache, client)
        scope = await repo.get_state(KEY_SCOPE)
        details = {
            **login,
            "scope": scope,
            "can_write": None if scope is None else ("media:write" in scope),
            "client_id_configured": bool(client.client_id),
            "user": user_info.get("user") if isinstance(user_info, dict) else None,
            "account": user_info.get("account") if isinstance(user_info, dict) else None,
        }
        return DriverStatus(driver=NAME, connected=bool(client.access_token), auth_state=login["auth_state"],
                            configured=bool(client.client_id), message=login["auth_message"], details=details)

    async def local_status(self) -> DriverStatus:
        """status() from what is stored and in memory only: no call to SIMKL (no account details), so no token is
        renewed or written. For readers that must not touch the account (the MCP sync_status tool)."""
        await self.service.ensure_credentials()
        client = self.client
        login = await auth.describe(self.repository, client, RENEW_MARGIN)
        return DriverStatus(driver=NAME, connected=bool(client.access_token), auth_state=login["auth_state"],
                            configured=bool(client.client_id), message=login["auth_message"], details={})

    async def connect(self, step: Optional[Mapping[str, Any]] = None) -> ConnectStep:
        """No step: starts the device login (a code to enter at simkl.com/pin). With the state of that step: checks
        whether it was approved, and stores the login when it was."""
        await self.service.ensure_credentials()
        client, repo = self.client, self.repository
        if step is None:
            try:
                data = await client.get_pin()
            except Exception as e:
                return ConnectStep(kind="error", message=f"Failed to get PIN: {e}")
            return ConnectStep(
                kind="code", code=data.get("user_code"),
                url=data.get("verification_uri_complete") or data.get("verification_uri") or data.get("verification_url"),
                interval=data.get("interval"), expires_in=data.get("expires_in"),
                state={k: data.get(k) for k in ("user_code", "device_code", "auth_type") if data.get(k) is not None})
        try:
            data = await client.poll_pin_token(user_code=step.get("user_code"), device_code=step.get("device_code"))
        except Exception as e:
            return ConnectStep(kind="error", message=f"Error polling PIN: {e}")
        if data.get("result") == "OK" and data.get("access_token"):
            token = data["access_token"]
            refresh = data.get("refresh_token")
            try:
                ttl = int(data.get("expires_in"))
            except (TypeError, ValueError):
                ttl = 0
            expires_at = time.time() + ttl if ttl > 0 else (time.time() + DEFAULT_TTL if refresh else None)
            async with client.login_change_lock():  # no older save may land after this one
                client.set_tokens(token, refresh, expires_at)
                await auth.persist_tokens(repo, token, refresh, expires_at, new_login=True)
            if data.get("scope"):
                await repo.set_state(KEY_SCOPE, data["scope"])
            else:
                await repo.delete_state(KEY_SCOPE)
            await repo.delete_state(KEY_LAST_ERROR)
            self.service.user_cache = (None, None)
            return ConnectStep(kind="done")
        return ConnectStep(kind="pending", state=dict(step), message=data.get("message"))

    async def set_credentials(self, values: Mapping[str, str]) -> Dict[str, Any]:
        """Stores an app id (client_id) and/or a pasted access token (access_token)."""
        client, repo = self.client, self.repository
        client_id = (values.get("client_id") or "").strip()
        access_token = (values.get("access_token") or "").strip()
        if client_id:
            await repo.set_state(KEY_CLIENT_ID, client_id)
            client.client_id = client_id
        if access_token:
            async with client.login_change_lock():  # no renewal of the previous login may be saved after this
                # A pasted token has no matching refresh token (set_tokens drops the old one: it would overwrite the
                # pasted token on the next renewal)
                client.set_tokens(access_token, None, None)
                await repo.set_state(KEY_ACCESS, access_token)
                await repo.delete_state(KEY_REFRESH)
                await repo.delete_state(auth.KEY_EXPIRES)
                await auth.clear_needs_reconnect(repo)
        return {"client_id_set": bool(client_id), "token_set": bool(access_token)}

    async def disconnect(self) -> None:
        client, repo = self.client, self.repository
        async with client.login_change_lock():  # no renewal of the old login may be saved after the deletes
            client.set_tokens(None, None, None)
            for key in (KEY_ACCESS, KEY_REFRESH, KEY_CURSOR, auth.KEY_EXPIRES, auth.KEY_RENEWED, auth.KEY_STATE,
                        auth.KEY_MSG, auth.KEY_SOFT):
                await repo.delete_state(key)
        self.service.user_cache = (None, None)

    async def diagnose(self) -> Dict[str, Any]:
        """What SIMKL answers (status and message) to a public and an authenticated call."""
        await self.service.ensure_credentials()
        c = self.client
        return {
            "client_id_set": bool(c.client_id),
            "client_id_preview": (c.client_id[:6] + "…") if c.client_id else None,
            "token_set": bool(c.access_token),
            "public_search": await c.probe("/search/tv", params={"q": "severance", "limit": 1}),
            "activities": await c.probe("/sync/activities"),
        }

    # --- pull -----------------------------------------------------------------------------------------------------

    def decode_entry(self, media_type: str, native: Any) -> Optional[LibraryEntry]:
        return mapper.parse_entry(native, media_type=MediaType(media_type)) if isinstance(native, dict) else None

    def _change(self, item: Dict[str, Any], media_type: MediaType) -> Optional[SyncChange]:
        sid = extract_simkl_id(mapper.item_obj(item))
        if not sid:
            return None  # nothing to stage or apply
        return SyncChange(kind="entry", media_type=media_type.value, ids={NAME: sid},
                          entry=mapper.parse_entry(item, media_type=media_type), native=item)

    async def _fetch_type(self, media: str, date_from: Optional[str] = None) -> List[Dict[str, Any]]:
        client = self.client
        try:
            data = await self.service._call(client.get_all_items(media, date_from=date_from, extended="full"))
            return data.get(media) or []
        except SimklError as e:
            if e.status_code == 400 and "max_items" in e.message:
                # Very large library: fetch one status at a time
                items: List[Dict[str, Any]] = []
                for status in STATUS_BUCKETS:
                    if media == "movies" and status in ("watching", "hold"):
                        continue
                    data = await self.service._call(
                        client.get_all_items(media, status=status, date_from=date_from, extended="full"))
                    items.extend(data.get(media) or [])
                return items
            raise

    async def _remote_ids(self, media: str) -> frozenset:
        data = await self.service._call(self.client.get_all_items(
            media, extended="simkl_ids_only", episode_watched_at=False, next_watch_info=False))
        return frozenset(sid for sid in (extract_simkl_id(mapper.item_obj(i)) for i in data.get(media) or []) if sid)

    @staticmethod
    def _list_moved(cur: Dict[str, Any], old: Dict[str, Any]) -> bool:
        """True if a list/status bucket (or a bare 'all' bump) changed, i.e. the items must be re-fetched."""
        return any(cur.get(b) != old.get(b) for b in STATUS_BUCKETS) or (
            cur.get("all") != old.get("all")
            and all(cur.get(b) == old.get(b) for b in ("rated_at", "removed_from_list", "playback"))
        )

    @staticmethod
    def _pull_plan(force_full: bool, current: Dict[str, Any], saved: Optional[Dict[str, Any]]):
        """("full", [(media, media_type)]), ("delta", [(media, media_type, cur, old)]) or ("none", [])."""
        if saved is None or force_full or not saved.get("all"):
            return "full", [(media, media_type) for media, (_, media_type) in SYNC_TYPE_INFO.items()]
        if current.get("all") == saved.get("all"):
            return "none", []
        changed = []
        for media, (act_key, media_type) in SYNC_TYPE_INFO.items():
            cur = current.get(act_key) or {}
            old = saved.get(act_key) or {}
            if cur != old:
                changed.append((media, media_type, cur, old))
        return "delta", changed

    async def plan_pull(self, cursor: Any, full: bool) -> PullPlan:
        """The cursor is SIMKL's /sync/activities answer as it stood after the last complete pull."""
        current = await self.service._call(self.client.get_activities())
        await auth.token_works(self.repository, self.client)  # an authenticated call just succeeded
        saved = cursor if isinstance(cursor, dict) else None
        mode, plan = self._pull_plan(full, current, saved)
        return PullPlan(mode=mode, cursor=current, detail=plan)

    async def pull(self, plan: PullPlan, cursor: Any) -> AsyncIterator[PullBatch]:
        if plan.mode == "full":
            for media, media_type in plan.detail:
                items = await self._fetch_type(media)
                keys = frozenset(sid for sid in (extract_simkl_id(mapper.item_obj(it)) for it in items) if sid)
                yield PullBatch(media_type=media_type.value, changes=self._changes(items, media_type), listing=True,
                                remote_keys=keys)
        elif plan.mode == "delta":
            date_from = cursor["all"]
            for media, media_type, cur, old in plan.detail:
                if self._list_moved(cur, old):
                    items = await self._fetch_type(media, date_from=date_from)
                    yield PullBatch(media_type=media_type.value, changes=self._changes(items, media_type))
                if cur.get("rated_at") != old.get("rated_at"):
                    data = await self.service._call(self.client.get_ratings(media, date_from=date_from))
                    yield PullBatch(media_type=media_type.value, changes=[
                        SyncChange(kind="rating", media_type=media_type.value, ids={NAME: sid},
                                   rating=it.get("user_rating"), entry=mapper.parse_entry(it, media_type=media_type),
                                   native=it)
                        for it, sid in ((it, extract_simkl_id(mapper.item_obj(it))) for it in data.get(media) or [])
                        if sid])
                if cur.get("removed_from_list") != old.get("removed_from_list"):
                    yield PullBatch(media_type=media_type.value, listing=True,
                                    remote_keys=await self._remote_ids(media))

    def _changes(self, items: List[Dict[str, Any]], media_type: MediaType) -> List[SyncChange]:
        return [c for c in (self._change(it, media_type) for it in items) if c is not None]

    async def entries(self, media_type: str) -> List[SyncChange]:
        mt = MediaType(media_type)
        return self._changes(await self._fetch_type(MEDIA_OF[media_type]), mt)

    # --- push -----------------------------------------------------------------------------------------------------

    def batch_key(self, op: SyncOp) -> Optional[Hashable]:
        return payloads.batch_key(op)

    async def push(self, ops: Sequence[SyncOp]) -> Any:
        try:
            path, body = payloads.request_for(list(ops))
        except ValueError as e:
            if len(ops) > 1:
                raise SyncError("unbatchable", str(e)) from e
            raise SyncError("rejected", str(e)) from e
        try:
            resp = await self.client.push(path, body)
        except SimklScopeError as e:
            raise SyncError("scope", READ_ONLY_MSG) from e
        except SimklAuthError as e:
            raise SyncError("auth", "SIMKL rejected the access token. Reconnect SIMKL.") from e
        except Exception as e:
            status = getattr(e, "status_code", 0)
            if status == 409:  # duplicate watch event = already recorded
                raise SyncError("duplicate", str(e)) from e
            if _is_transient(e):
                raise SyncError("transient", str(e)) from e
            raise SyncError("rejected", str(e), permanent=status in (400, 404, 422) and "rate_limit" not in str(e)) from e
        if len(ops) > 1:
            # SIMKL always answers with a not_found object, usually with empty lists ({"movies": [], "shows": []}):
            # only items in it mean something was not applied
            not_found = resp.get("not_found") if isinstance(resp, dict) else None
            missing = len(_not_found_items(not_found))
            if missing:
                # SIMKL ids only: the answer can quote titles, which stay out of the log (it is exported to Loki)
                logger.warning(f"SIMKL could not match {missing} of the {len(ops)} titles in a batched {ops[0].kind}; "
                               f"those changes were not applied there (SIMKL ids: {_simkl_ids(not_found)[:20]})")
        return resp


class SimklCatalog:
    """catalog: SIMKL's catalogue, by SIMKL id."""

    key_provider = NAME

    def __init__(self, service: SimklService):
        self.service = service

    async def ready(self) -> bool:
        await self.service.ensure_credentials()
        return bool(self.service.client.client_id)

    @staticmethod
    def parse_link(text: str) -> Optional[Tuple[str, int, str]]:
        """(NAME, id, media type) for a pasted link to a title on SIMKL; None for anything else."""
        m = re.search(r"simkl\.com/(tv|anime|movies)/(\d+)", text, re.I)
        return (NAME, int(m.group(2)), {"tv": "show", "anime": "anime", "movies": "movie"}[m.group(1).lower()]) if m else None

    async def search(self, query: str, media_types: Sequence[str], limit: int = SEARCH_LIMIT,
                     detailed: int = 8) -> List[Title]:
        """Searches TV, anime and movies (in the order of `media_types`), `limit` results of each, best match first
        (textsearch.rank_matches); the first `detailed` of them get their details."""
        kinds = tuple(dict.fromkeys(SEARCH_KINDS[m] for m in media_types if m in SEARCH_KINDS))

        def order(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            return rank_matches(query, items, names=lambda i: title_names(mapper.search_title(i)),
                                year=lambda i: mapper.search_title(i).year)

        return [mapper.search_title(item) for item in
                await self.service.client.search(query, kinds=kinds or ("tv", "anime", "movie"), limit=limit,
                                                 enrich=detailed, order=order)]

    async def title(self, media_type: Optional[str], ids: Mapping[str, Any]) -> Optional[Title]:
        """Details by SIMKL id; with no media type, each type is tried. None when SIMKL has nothing."""
        sid = ids.get(NAME)
        if not sid:
            return None
        order = [MediaType(media_type)] if media_type else [MediaType.SHOW, MediaType.ANIME, MediaType.MOVIE]
        for mt in order:
            try:
                record = await self.service.client.get_details(DETAIL_MEDIA[mt.value], sid)
            except Exception as e:
                logger.debug(f"Details for SIMKL {sid} failed: {e}")
                record = {}
            if record and (record.get("title") or media_type):
                return mapper.details_title(record, mt)
        return None

    async def episodes(self, media_type: str, ids: Mapping[str, Any]) -> Optional[List[Episode]]:
        media = "anime" if media_type == "anime" else "tv"
        return mapper.parse_episodes(await self.service.client.get_episodes(media, ids[NAME]), media)

    async def related(self, media_type: str, ids: Mapping[str, Any]) -> Optional[List[RelatedTitle]]:
        return mapper.extract_sequels(await self.service.client.get_details("anime", ids[NAME]))  # raising = failed

    async def resolve(self, media_type: str, ids: Mapping[str, Any]) -> Optional[Tuple[Dict[str, Any], str]]:
        """A SIMKL id for a title known by its TMDB id (and whether SIMKL files it as anime)."""
        if ids.get(NAME):
            return dict(ids), media_type
        provider = next((p for p in ("tmdb", "imdb") if ids.get(p)), None)
        if not provider:
            return None
        await self.service.ensure_credentials()
        query = {provider: ids[provider]}
        if provider == "tmdb":  # an IMDb id says movie or show by itself
            query["type"] = "movie" if media_type == "movie" else "show"
        for hit in await self.service.client.lookup_by_id(**query):
            sid = extract_simkl_id(hit) or extract_simkl_id(hit.get("show") or hit.get("movie") or {})
            if sid:
                anime = hit.get("type") == "anime" or bool(hit.get("anime_type"))
                return {**ids, NAME: sid}, ("anime" if anime else media_type)
        return None


class SimklSchedule:
    """schedule: what airs soon, for the titles on the account's lists (and the airing lists)."""

    def __init__(self, service: SimklService):
        self.service = service

    async def upcoming(self) -> List[ScheduledEpisode]:
        client = self.service.client
        airing = (await client.get_airing_tv()) + (await client.get_airing_anime())
        return mapper.schedule_entries(airing, await client.get_upcoming_calendar())


class SimklDiscovery:
    """discovery: SIMKL's trending lists and its per-title recommendations."""

    key_provider = NAME

    def __init__(self, service: SimklService):
        self.service = service

    async def ready(self) -> bool:
        await self.service.ensure_credentials()
        return bool(self.service.client.client_id)

    async def trending(self, media_type: str, interval: str) -> List[Title]:
        media = {"show": "tv", "anime": "anime", "movie": "movies"}[media_type]
        raw = await self.service.client.get_trending(media, interval)
        return [t for t in (mapper.catalog_item(r, media_type) for r in raw[:60]) if t]

    async def recommendations(self, media_type: str, ids: Mapping[str, Any]) -> List[Tuple[Title, float]]:
        """"Users also watched" (weight 1.5) and "similar" (1.0), better ranks scoring more."""
        try:
            d = await self.service.client.get_details(DETAIL_MEDIA[media_type], ids[NAME])
        except Exception as e:
            logger.debug(f"SIMKL recommendations for {ids.get(NAME)} failed: {e}")
            return []
        out: List[Tuple[Title, float]] = []
        for weight, key in ((1.5, "users_recommendations"), (1.0, "similar")):
            for rank, r in enumerate(d.get(key) or []):
                kind = str(r.get("type") or "").lower()
                mt = "movie" if kind.startswith("movie") else "anime" if kind == "anime" else "show"
                item = mapper.catalog_item(r, mt)
                if item and item.ids.get(NAME):
                    out.append((item, weight * (1.0 + (12 - min(rank, 12)) / 12.0)))
        return out


__all__ = ["SimklService", "SimklLibrary", "SimklCatalog", "SimklSchedule", "SimklDiscovery", "normal_ids"]
