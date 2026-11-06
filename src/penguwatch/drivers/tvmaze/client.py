"""HTTP client for the TVmaze API (https://www.tvmaze.com/api): no key, at least 20 calls per 10 seconds per address.

One shared connection (TVmaze blocks addresses that hold several idle ones), a rate limiter under the published
limit, a descriptive User-Agent, and a retry with back-off on 429 and 5xx (honouring Retry-After)."""

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional

import httpx

from penguwatch import telemetry
from penguwatch.drivers.ratelimit import RateLimiter

logger = logging.getLogger("penguwatch.drivers.tvmaze.client")

BASE_URL = "https://api.tvmaze.com"
MAX_RETRY_WAIT = 30.0


class TvmazeError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"TVmaze HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class TvmazeClient:
    def __init__(self, *, app_version: str = "0", timeout_seconds: float = 20.0, rate_calls: int = 18,
                 rate_window: float = 10.0, retries: int = 2, retry_base_delay: float = 3.0,
                 limiter: Optional[RateLimiter] = None, sleep: Callable[[float], Any] = asyncio.sleep):
        self.user_agent = f"PenguWatch/{app_version} (self-hosted; +https://www.tvmaze.com/api)"
        self.timeout = timeout_seconds
        self.retries = retries
        self.retry_base_delay = retry_base_delay
        self.limiter = limiter or RateLimiter(rate_calls, rate_window)
        self._sleep = sleep
        self._http: Optional[httpx.AsyncClient] = None
        self.on_request = None  # callback(service, path, http_status, seconds); status 0 = network error

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True,  # /lookup answers with a redirect to the show
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
                headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    def _observe(self, path: str, status: int, seconds: float) -> None:
        if self.on_request:
            try:
                self.on_request("tvmaze", path, status, seconds)
            except Exception:  # telemetry must never break a call
                pass

    async def _send(self, path: str, params: Optional[Dict[str, Any]], attempt: int) -> httpx.Response:
        await self.limiter.wait()
        t0 = time.perf_counter()
        try:
            with telemetry.upstream_call("TVmaze", "api.tvmaze.com", "GET", path, attempt) as done:
                resp = await self._client().get(f"{BASE_URL}{path}", params=params)
                done(resp.status_code)
            self._observe(path, resp.status_code, time.perf_counter() - t0)
        except httpx.TransportError:
            self._observe(path, 0, time.perf_counter() - t0)
            raise
        return resp

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else self.retry_base_delay * attempt
        except ValueError:
            delay = self.retry_base_delay * attempt
        return min(delay, MAX_RETRY_WAIT)

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None, *, missing_ok: bool = False) -> Any:
        """The JSON answer of GET `path`. A 404 is None when `missing_ok` (a lookup that found nothing), else an
        error; 429 and 5xx are retried; anything else raises TvmazeError (status 0: the network)."""
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = await self._send(path, params, attempt)
            except httpx.TransportError as e:
                if attempt <= self.retries:
                    await self._sleep(self.retry_base_delay * attempt * 0.5)
                    continue
                raise TvmazeError(0, f"network error: {e}") from e
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    raise TvmazeError(200, "the answer is not JSON") from None
            if resp.status_code == 404 and missing_ok:
                return None
            if resp.status_code in (429, 500, 502, 503, 504) and attempt <= self.retries:
                delay = self._retry_delay(resp, attempt)
                logger.debug(f"TVmaze answered {resp.status_code} for {path}; waiting {delay:g}s")
                await self._sleep(delay)
                continue
            raise TvmazeError(resp.status_code, resp.text[:200])
