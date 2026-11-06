"""Async HTTP client for the SIMKL API (https://api.simkl.org)."""

import asyncio
import time
import logging
from typing import Dict, Any, Optional, List
import httpx

from penguwatch import telemetry

logger = logging.getLogger("penguwatch.drivers.simkl.client")

SIMKL_BASE_URL = "https://api.simkl.com"
SIMKL_DATA_URL = "https://data.simkl.in"

# SIMKL media "types" as used in /sync/all-items/{type}
SYNC_TYPES = ("shows", "anime", "movies")
# Without media:write SIMKL issues a read-only token and every push fails with 403 insufficient_scope
OAUTH_SCOPE = "media:read media:write"
# Assumed token lifetime when SIMKL issues a refresh token without saying when the access token expires
DEFAULT_TTL = 7 * 86400
RENEW_MARGIN = 86400  # renew proactively when less than this is left
REFRESH_BACKOFF = 60  # seconds without a refresh attempt after a transient token-endpoint failure
# OAuth error codes that mean SIMKL really refused the refresh token (anything else is a passing problem)
REFUSED_ERRORS = frozenset({"invalid_grant", "invalid_client", "unauthorized_client"})


class SimklError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"SIMKL HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class SimklAuthError(SimklError):
    """401: token missing, expired, or the user revoked the app."""


class SimklScopeError(SimklError):
    """403 insufficient_scope: the login is read-only (needs media:write)."""


def poster_url(p: Optional[str]) -> Optional[str]:
    if not p:
        return None
    return p if str(p).startswith("http") else f"https://simkl.in/posters/{p}_m.jpg"


def fanart_url(f: Optional[str]) -> Optional[str]:
    if not f:
        return None
    return f if str(f).startswith("http") else f"https://simkl.in/fanart/{f}_medium.jpg"


def extract_simkl_id(obj: Dict[str, Any]) -> Optional[int]:
    if not isinstance(obj, dict):
        return None
    ids = obj.get("ids") or {}
    sid = ids.get("simkl") or ids.get("simkl_id") or obj.get("simkl_id") or obj.get("simkl")
    try:
        return int(sid) if sid else None
    except (TypeError, ValueError):
        return None


class SimklClient:
    def __init__(
        self,
        client_id: str,
        access_token: Optional[str] = None,
        app_name: str = "PenguWatch",
        app_version: str = "1.0.0",
        timeout_seconds: float = 20.0,
    ):
        self.client_id = client_id
        self.access_token = access_token
        self.app_name = app_name
        self.app_version = app_version
        self.timeout = timeout_seconds
        self.retry_base_delay = 3.0
        self.refresh_token: Optional[str] = None
        self.token_expires_at: Optional[float] = None  # epoch seconds; None = unknown
        self.refresh_rejected = False  # SIMKL refused the refresh token: only a new login helps
        self.last_refresh_outcome: Optional[str] = None  # "ok" | "transient" | "rejected"
        self.on_token_refreshed = None  # async callback(access_token, refresh_token, expires_at) to persist new tokens
        # async callback(expires_at, still_current) when SIMKL's token endpoint rejected the refresh token;
        # still_current() says whether the login that was refused is still the installed one (re-check before writing)
        self.on_auth_rejected = None
        # Single-flight: concurrent 401s share one refresh task, which the client owns (callers only wait for it,
        # behind asyncio.shield, so a cancelled caller never cancels an exchange that may already have reached SIMKL).
        # The token callbacks (on_token_refreshed / on_auth_rejected) must never call SIMKL or the client's refresh:
        # they would wait for the very task they run in.
        self._refresh_task: Optional["asyncio.Task[str]"] = None
        self._refresh_gen = 0  # the login generation the current/last refresh task belongs to
        # Bumped by every set_tokens (new login, pasted token, disconnect). A refresh whose answer arrives after the
        # login changed is discarded: it belongs to a login that no longer exists.
        self._login_gen = 0
        # A renewal whose save failed: (login generation, access, refresh, expires_at). The new tokens are live in
        # memory, but a restart would lose them (refresh tokens may be single-use), so the save is retried on every
        # renewal check until it succeeds.
        self._pending_persist: Optional[tuple] = None
        self._persist_lock = asyncio.Lock()
        self._next_renew_try = 0.0  # time.monotonic() before which renew_if_due backs off
        self._refresh_backoff_until = 0.0  # time.monotonic() before which no refresh is attempted after a transient error
        self._http: Optional[httpx.AsyncClient] = None
        self.on_request = None  # callback(service, path, http_status, seconds); status 0 = network error

    def set_tokens(self, access_token: Optional[str], refresh_token: Optional[str] = None,
                   expires_at: Optional[float] = None) -> None:
        """Installs a login. It is authoritative: the refresh token and expiry become exactly what is given (None
        included), because a refresh token from an earlier login does not belong to this access token. A refresh
        still in flight for the previous login is invalidated."""
        self._login_gen += 1
        self._pending_persist = None  # a save still owed for the previous login must not overwrite this one
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_expires_at = expires_at
        self.refresh_rejected = False
        self._next_renew_try = 0.0
        self._refresh_backoff_until = 0.0

    # --- plumbing ---------------------------------------------------------

    def _headers(self, require_auth: bool = True) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "simkl-api-key": self.client_id or "",
            "User-Agent": f"{self.app_name}/{self.app_version}",
        }
        # Some SIMKL apps require the user token on *every* request (even search/details),
        # so it's sent whenever we have one; require_auth=False is only used for the login flow.
        if require_auth and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _std_params(self) -> Dict[str, str]:
        # client_id on every request (same as the original, known-working client)
        return {"client_id": self.client_id or ""}

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._http

    async def aclose(self):
        task = self._refresh_task
        if task is not None and not task.done():  # let an exchange that already reached SIMKL finish and save its tokens
            await asyncio.wait({task}, timeout=10)
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    def _observe(self, path: str, status: int, seconds: float) -> None:
        if self.on_request:
            try:
                self.on_request("simkl", path, status, seconds)
            except Exception:  # telemetry must never break a call
                pass

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        auth: bool = True,
        retries: int = 2,
        base: str = SIMKL_BASE_URL,
    ) -> Any:
        url = path if path.startswith("http") else f"{base}{path}"
        q = self._std_params()
        if params:
            q.update({k: v for k, v in params.items() if v is not None})
        attempt = 0
        refreshed = False
        while True:
            attempt += 1
            sent = await self._renew_if_expired(auth)
            try:
                resp = await self._send(method, url, path, q, json_body, auth, attempt)
            except httpx.TransportError as e:
                if attempt <= retries:
                    await asyncio.sleep(0.5 * self.retry_base_delay * attempt)
                    continue
                raise SimklError(0, f"network error: {e}") from e

            if resp.status_code in (200, 201, 204):
                return self._decode(resp)

            body_text = resp.text[:300]
            if resp.status_code == 401:
                if auth and self.refresh_token and not refreshed:
                    refreshed = True
                    if await self.refresh_access_token(stale_token=sent):
                        continue
                raise SimklAuthError(401, body_text)
            if resp.status_code == 403 and "insufficient_scope" in body_text:
                raise SimklScopeError(403, body_text)
            delay = self._retry_delay(resp, body_text, attempt) if attempt <= retries else None
            if delay is not None:
                await asyncio.sleep(delay)
                continue
            raise SimklError(resp.status_code, body_text)

    # The three steps of _request: the token, the transport and the retry policy.

    async def _renew_if_expired(self, auth: bool) -> Optional[str]:
        """The access token to send. A token known to be (about to be) expired is renewed first instead of burning a
        401."""
        sent = self.access_token
        if (auth and self.refresh_token and not self.refresh_rejected and self.token_expires_at
                and time.time() >= self.token_expires_at - 60):
            await self.refresh_access_token(stale_token=sent)
            sent = self.access_token
        return sent

    async def _send(self, method: str, url: str, path: str, params: Dict[str, Any], json_body: Any, auth: bool,
                    attempt: int) -> httpx.Response:
        """One HTTP exchange, traced and reported to on_request (status 0 for a network error, which is raised)."""
        t0 = time.perf_counter()
        try:
            with telemetry.upstream_call("SIMKL", "api.simkl.com", method, path, attempt) as done:
                resp = await self._client().request(
                    method, url, params=params, json=json_body, headers=self._headers(require_auth=auth)
                )
                done(resp.status_code)
            self._observe(path, resp.status_code, time.perf_counter() - t0)
        except httpx.TransportError:
            self._observe(path, 0, time.perf_counter() - t0)
            raise
        return resp

    @staticmethod
    def _decode(resp: httpx.Response) -> Any:
        """The JSON body of a successful answer; {} when there is none or it is not JSON."""
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    def _retry_delay(self, resp: httpx.Response, body_text: str, attempt: int) -> Optional[float]:
        """Seconds to wait before trying again, or None when the answer is not worth a retry. SIMKL serialises writes
        per user with a 20s lock -> 400 {"error":"rate_limit"}."""
        retryable = resp.status_code in (429, 500, 502, 503, 504) or (
            resp.status_code == 400 and "rate_limit" in body_text
        )
        if not retryable:
            return None
        retry_after = resp.headers.get("Retry-After")
        delay = float(retry_after) if retry_after and retry_after.isdigit() else self.retry_base_delay * attempt
        return min(delay, 30.0)

    async def probe(self, path: str, auth: bool = True, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Raw status/body of one GET (for the diagnostics endpoint). Never returns the token."""
        try:
            resp = await self._client().get(
                f"{SIMKL_BASE_URL}{path}", params={**self._std_params(), **(params or {})}, headers=self._headers(require_auth=auth)
            )
            return {"path": path, "auth": auth, "status": resp.status_code, "body": resp.text[:300]}
        except Exception as e:
            return {"path": path, "auth": auth, "status": None, "body": str(e)[:300]}

    # --- authentication (PIN / device flow) --------------------------------

    async def get_pin(self) -> Dict[str, Any]:
        """Starts the device authorization flow. Tries OAuth 2.0 device flow, then the classic PIN endpoint."""
        if not self.client_id:
            raise ValueError("SIMKL Client ID is required for PIN flow.")
        client = self._client()
        form_headers = {k: v for k, v in self._headers(require_auth=False).items() if k != "Content-Type"}
        try:
            resp = await client.post(
                f"{SIMKL_BASE_URL}/oauth2/device",
                data={"client_id": self.client_id, "scope": OAUTH_SCOPE},
                headers=form_headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                data["auth_type"] = "oauth2_device"
                return data
        except Exception as e:
            logger.debug(f"OAuth2 device flow init failed, trying classic PIN: {e}")

        data = await self._request(
            "GET", "/oauth/pin", params={"redirect": "urn:ietf:wg:oauth:2.0:oob", "scope": OAUTH_SCOPE}, auth=False
        )
        data["auth_type"] = "legacy_pin"
        data.setdefault("verification_uri", data.get("verification_url") or "https://simkl.com/pin")
        return data

    def _expiry_from(self, data: Dict[str, Any]) -> Optional[float]:
        now = time.time()
        try:
            ttl = int(data.get("expires_in"))
        except (TypeError, ValueError):
            ttl = 0
        if ttl > 0:
            return now + ttl
        if data.get("refresh_token") or self.refresh_token:
            return now + DEFAULT_TTL
        return None

    async def refresh_access_token(self, stale_token: Optional[str] = None) -> bool:
        """OAuth refresh (only possible when SIMKL issued a refresh_token). Single-flight.

        `stale_token` is the access token the caller just saw rejected; if another request has
        already replaced it, that refresh is reused (refresh tokens may be single-use, so a second
        refresh would be rejected and kill the login). If the login was replaced while the refresh was in
        flight, its answer is discarded and the result reflects the current login."""
        outcome = await self._refresh(stale_token)
        if outcome == "superseded":
            return bool(self.access_token)
        return outcome == "ok"

    async def _refresh(self, stale_token: Optional[str]) -> str:
        """Returns "ok" | "failed" | "superseded"."""
        task = self._refresh_task
        if task is None or task.done() or self._refresh_gen != self._login_gen:
            if stale_token is not None and self.access_token != stale_token:
                return "ok"
            if not self.refresh_token or self.refresh_rejected:
                return "failed"
            if time.monotonic() < self._refresh_backoff_until:
                return "failed"  # the token endpoint failed recently: don't retry on every request
            self._refresh_gen = self._login_gen
            task = self._refresh_task = asyncio.create_task(self._exchange(self._login_gen, self.refresh_token))
            task.add_done_callback(self._count_refresh)
        # The caller may be cancelled; the exchange (which owns the rotated refresh token) must not be.
        return await asyncio.shield(task)

    def _count_refresh(self, task: "asyncio.Task") -> None:
        if not task.cancelled() and task.exception() is None:
            result = task.result()
            outcome = "superseded" if result == "superseded" else (self.last_refresh_outcome or result)
            telemetry.TOKEN_REFRESHES.add(1, {"driver": "simkl", "outcome": outcome})

    @telemetry.traced("simkl.token.refresh", outcome=True)
    async def _exchange(self, gen: int, refresh_token: str) -> str:
        """The refresh POST, the state update and the save, as one unit. Never raises."""
        try:
            resp = await self._client().post(
                f"{SIMKL_BASE_URL}/oauth2/token",
                data={"client_id": self.client_id, "grant_type": "refresh_token", "refresh_token": refresh_token},
                headers={k: v for k, v in self._headers(require_auth=False).items() if k != "Content-Type"},
            )
            try:
                data = resp.json() if resp.content else {}
            except ValueError:
                data = {}
            if not isinstance(data, dict):
                data = {}
        except Exception as e:
            if gen != self._login_gen:
                return "superseded"
            logger.warning(f"SIMKL token refresh failed (will retry later): {e}")
            return self._refresh_transient()
        if gen != self._login_gen:
            logger.info("Discarding a SIMKL token refresh answer: the login changed while it was in flight.")
            return "superseded"
        status = resp.status_code
        if status == 200 and data.get("access_token"):
            self.access_token = data["access_token"]
            self.refresh_token = data.get("refresh_token") or self.refresh_token
            self.token_expires_at = self._expiry_from(data)
            self.last_refresh_outcome = "ok"
            self._refresh_backoff_until = 0.0
            self._pending_persist = (gen, self.access_token, self.refresh_token, self.token_expires_at)
            await self.flush_pending_persist()
            logger.info("SIMKL access token refreshed.")
            return "ok"
        if self._refresh_refused(status, data):
            logger.warning(f"SIMKL token refresh rejected: HTTP {status}")
            self.refresh_rejected = True
            self.last_refresh_outcome = "rejected"
            if self.on_auth_rejected:
                try:
                    await self.on_auth_rejected(self.token_expires_at, lambda: gen == self._login_gen)
                except Exception as e:
                    logger.error(f"Could not record the failed SIMKL renewal: {e}", exc_info=True)
            return "failed"
        # Anything else (5xx, 429, a Cloudflare page, a rate-limit error, an odd 200) says nothing about the refresh token
        logger.warning(f"SIMKL token refresh failed (HTTP {status}); will retry later.")
        return self._refresh_transient()

    def _refresh_transient(self) -> str:
        self.last_refresh_outcome = "transient"
        self._refresh_backoff_until = time.monotonic() + REFRESH_BACKOFF
        return "failed"

    @staticmethod
    def _refresh_refused(status: int, data: Dict[str, Any]) -> bool:
        """Did SIMKL's token endpoint refuse the refresh token? Only an OAuth-style JSON error says so: a 400/403 that
        is an HTML page (Cloudflare), a rate-limit or any other error code is a passing problem, not a dead login."""
        if status not in (400, 401, 403):
            return False
        error = data.get("error")
        if not isinstance(error, str) or not error:
            return False
        return error in REFUSED_ERRORS or status == 401

    def login_change_lock(self) -> asyncio.Lock:
        """Held while a login is installed AND stored (set_tokens + persist), and while a renewal is being stored, so
        a slow or retried save of an older login can never land after a newer login's save."""
        return self._persist_lock

    async def flush_pending_persist(self) -> bool:
        """Saves the latest renewed tokens if that has not succeeded yet. Never raises; True when nothing is left to save."""
        async with self._persist_lock:
            pending = self._pending_persist
            if pending is None:
                return True
            if pending[0] != self._login_gen or not self.on_token_refreshed:
                self._pending_persist = None  # belongs to a login that was replaced (or nothing stores tokens)
                return True
            try:
                await self.on_token_refreshed(pending[1], pending[2], pending[3])
            except Exception as e:
                logger.error(f"Could not persist the refreshed SIMKL token (will retry on the next tick): {e}", exc_info=True)
                return False
            if self._pending_persist is pending:
                self._pending_persist = None
            return True

    async def renew_if_due(self, margin: float = RENEW_MARGIN) -> str:
        """Renews the access token ahead of expiry. Never raises.

        Returns no_refresh_token | rejected | throttled | not_needed | renewed | failed.
        A token with unknown expiry counts as due, so pre-existing logins renew once and learn it.
        A renewal whose save failed earlier is saved first."""
        try:
            await self.flush_pending_persist()
            if not self.access_token or not self.refresh_token:
                return "no_refresh_token"
            if self.refresh_rejected:
                return "rejected"
            if time.monotonic() < self._next_renew_try:
                return "throttled"
            if self.token_expires_at is not None and self.token_expires_at - time.time() > margin:
                return "not_needed"
            outcome = await self._refresh(self.access_token)
            if outcome == "ok":
                return "renewed"
            if outcome == "superseded":
                return "not_needed"  # a new login arrived meanwhile; it has its own schedule
            self._next_renew_try = time.monotonic() + 900
            return "rejected" if self.refresh_rejected else "failed"
        except Exception as e:  # the worker must never die over this
            logger.error(f"SIMKL renewal check failed: {e}", exc_info=True)
            return "failed"

    async def poll_pin_token(
        self,
        user_code: Optional[str] = None,
        device_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Polls for the access token while the user approves on simkl.com/pin."""
        client = self._client()
        if device_code:
            payload = {
                "client_id": self.client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            }
            form_headers = {k: v for k, v in self._headers(require_auth=False).items() if k != "Content-Type"}
            resp = await client.post(f"{SIMKL_BASE_URL}/oauth2/token", data=payload, headers=form_headers)
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code == 200 and "access_token" in data:
                return {
                    "result": "OK",
                    "access_token": data["access_token"],
                    "refresh_token": data.get("refresh_token"),
                    "scope": data.get("scope"),
                    "expires_in": data.get("expires_in"),
                }
            if data.get("error") in ("authorization_pending", "slow_down"):
                return {"result": "KO", "message": data["error"]}
            if not user_code:
                return {"result": "KO", "message": data.get("error_description") or data.get("error", "Unknown error")}

        if user_code:
            try:
                return await self._request("GET", f"/oauth/pin/{user_code}", auth=False, retries=0)
            except SimklError as e:
                return {"result": "KO", "message": e.message}

        return {"result": "KO", "message": "Neither device_code nor user_code provided"}

    # --- account / sync reads ---------------------------------------------

    async def get_user_settings(self) -> Dict[str, Any]:
        # SIMKL documents this as POST; GET is accepted by older deployments
        try:
            return await self._request("POST", "/users/settings", retries=0)
        except SimklError as e:
            if isinstance(e, SimklAuthError):
                raise
            return await self._request("GET", "/users/settings", retries=0)

    async def get_activities(self) -> Dict[str, Any]:
        return await self._request("GET", "/sync/activities")

    async def get_all_items(
        self,
        media: str,
        status: Optional[str] = None,
        date_from: Optional[str] = None,
        extended: Optional[str] = "full",
        episode_watched_at: bool = True,
        next_watch_info: bool = True,
    ) -> Dict[str, Any]:
        """GET /sync/all-items/{media}[/{status}] — the user's library (or a delta with date_from)."""
        path = f"/sync/all-items/{media}" + (f"/{status}" if status else "")
        params: Dict[str, Any] = {}
        if extended:
            params["extended"] = extended
        if date_from:
            params["date_from"] = date_from
        if extended == "full" and episode_watched_at:
            params["episode_watched_at"] = "yes"
        if next_watch_info and media != "movies" and extended not in ("simkl_ids_only", "ids_only"):
            params["next_watch_info"] = "yes"
        data = await self._request("GET", path, params=params)
        return data if isinstance(data, dict) else {}

    async def get_ratings(self, media: str, date_from: Optional[str] = None) -> Dict[str, Any]:
        data = await self._request("GET", f"/sync/ratings/{media}", params={"date_from": date_from})
        return data if isinstance(data, dict) else {}

    # --- catalogue ---------------------------------------------------------

    async def get_details(self, media: str, simkl_id: int) -> Dict[str, Any]:
        """media: 'tv' | 'anime' | 'movies'"""
        data = await self._request("GET", f"/{media}/{simkl_id}", params={"extended": "full"}, retries=1)
        return data if isinstance(data, dict) else {}

    async def get_episodes(self, media: str, simkl_id: int) -> List[Dict[str, Any]]:
        """media: 'tv' | 'anime'. Returns SIMKL's episode list (season/episode/title/date/aired)."""
        try:
            data = await self._request("GET", f"/{media}/episodes/{simkl_id}", params={"extended": "full"}, retries=1)
        except SimklError as e:
            logger.debug(f"Episode list for {media}/{simkl_id} failed: {e}")
            return []
        return data if isinstance(data, list) else []

    async def search(self, query: str, kinds=("tv", "anime", "movie"), limit: int = 10, enrich: int = 8) -> List[Dict[str, Any]]:
        """Searches the SIMKL catalogue. Each result gets SIMKL's id in 'ids' and 'media_type' (show/anime/movie)."""
        kind_media = {"tv": "show", "anime": "anime", "movie": "movie"}
        results: List[Dict[str, Any]] = []
        seen = set()

        async def one(kind: str) -> Any:
            try:
                return await self._request(
                    "GET", f"/search/{kind}", params={"q": query, "limit": limit, "extended": "full"}, retries=0
                )
            except SimklError as e:
                logger.warning(f"SIMKL search ({kind}) failed: {e}")
                return []

        # One person is waiting on this, so the kinds run side by side (no pacing); results keep the order of `kinds`.
        datas = await asyncio.gather(*(one(k) for k in kinds))
        for kind, data in zip(kinds, datas, strict=True):
            for item in data if isinstance(data, list) else []:
                sid = extract_simkl_id(item)
                if not sid or (kind, sid) in seen:
                    continue
                seen.add((kind, sid))
                item.setdefault("ids", {})["simkl"] = sid
                item["media_type"] = kind_media[kind]
                if kind == "anime":
                    item["is_anime"] = True
                results.append(item)

        detail_media = {"show": "tv", "anime": "anime", "movie": "movies"}

        async def enrich_item(it: Dict[str, Any]):
            try:
                dj = await self.get_details(detail_media[it["media_type"]], it["ids"]["simkl"])
            except Exception as e:
                logger.debug(f"Failed to enrich search item {it['ids'].get('simkl')}: {e}")
                return
            for key in ("network", "poster", "overview", "total_episodes", "genres", "runtime", "status", "year"):
                if not it.get(key) and dj.get(key):
                    it[key] = dj[key]
            if isinstance(it.get("ids"), dict) and isinstance(dj.get("ids"), dict):
                for k, v in dj["ids"].items():
                    it["ids"].setdefault(k, v)
            r_obj = dj.get("ratings")
            if isinstance(r_obj, dict):
                it.setdefault("ratings", {}).update(r_obj)

        if results and enrich:
            await asyncio.gather(*[enrich_item(it) for it in results[:enrich]])
        return results

    async def lookup_by_id(self, **ids) -> List[Dict[str, Any]]:
        """GET /search/id?imdb=...|tmdb=...&type=show|movie — resolve external ids to SIMKL ids."""
        try:
            data = await self._request("GET", "/search/id", params=ids, retries=0)
        except SimklError:
            return []
        return data if isinstance(data, list) else ([data] if isinstance(data, dict) and data else [])

    async def get_trending(self, media: str = "tv", interval: str = "week", size: int = 100) -> List[Dict[str, Any]]:
        """Public trending lists (CDN files, not rate limited). media: tv | anime | movies."""
        candidates = [
            f"{SIMKL_DATA_URL}/discover/trending/{media}/{interval}_{size}.json",
            f"{SIMKL_DATA_URL}/discover/trending/{media}/{interval}_100.json",
            f"{SIMKL_DATA_URL}/discover/trending/{media}/{interval}_500.json",
        ]
        for url in dict.fromkeys(candidates):
            try:
                data = await self._request("GET", url, auth=False, retries=0)
            except SimklError as e:
                logger.debug(f"Trending fetch {url} failed: {e}")
                continue
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ("items", "results", media, "tv", "anime", "movies"):
                    if isinstance(data.get(key), list):
                        return data[key]
        return []

    async def get_upcoming_calendar(self) -> Dict[str, Any]:
        """Upcoming TV + anime releases for the user's list ({"new_episodes": {day: {...}}})."""
        combined: Dict[str, Any] = {"new_episodes": {}}
        if not self.access_token:
            return combined
        for name in ("tv", "anime"):
            try:
                data = await self._request("GET", f"/{name}/calendar", retries=0)
            except SimklError as e:
                logger.debug(f"Calendar {name} failed: {e}")
                continue
            if not isinstance(data, dict):
                continue
            for day_k, day_v in (data.get("new_episodes") or {}).items():
                if day_k not in combined["new_episodes"]:
                    combined["new_episodes"][day_k] = day_v
                else:
                    existing = (combined["new_episodes"][day_k].get("items") or {}).get("all", [])
                    extra = ((day_v or {}).get("items") or {}).get("all", [])
                    combined["new_episodes"][day_k].setdefault("items", {})["all"] = existing + extra
        return combined

    async def _get_airing(self, media: str) -> List[Dict[str, Any]]:
        try:
            data = await self._request("GET", f"/{media}/airing", retries=0)
        except SimklError as e:
            logger.debug(f"/{media}/airing failed: {e}")
            return []
        return data if isinstance(data, list) else []

    async def get_airing_tv(self) -> List[Dict[str, Any]]:
        return await self._get_airing("tv")

    async def get_airing_anime(self) -> List[Dict[str, Any]]:
        return await self._get_airing("anime")

    # --- sync writes ----------------------------------------------------------

    async def push(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST a change to one of the /sync write endpoints."""
        return await self._request("POST", path, json_body=payload)

    async def add_to_history(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("POST", "/sync/history", json_body=payload)

    async def remove_from_history(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("POST", "/sync/history/remove", json_body=payload)

    async def add_to_list(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("POST", "/sync/add-to-list", json_body=payload)

    async def set_ratings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("POST", "/sync/ratings", json_body=payload)

    async def remove_ratings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("POST", "/sync/ratings/remove", json_body=payload)
