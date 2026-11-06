"""A sliding-window rate limiter for a driver's HTTP client: at most `calls` requests in any `per` seconds.

The clock and the sleep are parameters so tests run it with a fake clock."""

import asyncio
import time
from collections import deque
from typing import Awaitable, Callable, Deque


class RateLimiter:
    def __init__(self, calls: int, per: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        if calls < 1 or per <= 0:
            raise ValueError("a rate limit needs at least one call per positive period")
        self.calls = calls
        self.per = per
        self._clock = clock
        self._sleep = sleep
        self._starts: Deque[float] = deque()
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        """Returns when a request may be sent now, and counts it. Callers are served in order."""
        async with self._lock:
            while True:
                now = self._clock()
                while self._starts and now - self._starts[0] >= self.per:
                    self._starts.popleft()
                if len(self._starts) < self.calls:
                    self._starts.append(now)
                    return
                await self._sleep(self._starts[0] + self.per - now)
