"""Tracks when a person last used the app, so the background sync timer can back off while nobody does.

Services ask clients not to run unconditional polling timers without active user interaction. An open
tab that only refreshes itself (status badge, Up Next, System tab) must therefore not count as use.
"""

import re

import time
from typing import Callable, Optional

# Requests the open page makes on its own; they say nothing about a person being there.
PASSIVE_GET_PATHS = ("/api/up-next",)
PASSIVE_GET_RE = re.compile(r"^/api/drivers/[^/]+/status$")  # the login status badge
# Reports the page sends on its own (slow-request timings); a slow background poll must not end the idle period.
PASSIVE_POST_PATHS = ("/api/client-timing",)
# Operational endpoints (probes, scrapers, the System tab refresh).
OPERATIONAL_PREFIXES = ("/api/health", "/api/system", "/health", "/metrics")
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def is_user_interaction(method: str, path: str) -> bool:
    """True for a request that shows a person is using the app: the page itself or an /api/* call,
    minus health/metrics/system and the two endpoints the page polls every minute."""
    path = path.split("?", 1)[0].rstrip("/") or "/"
    if path != "/" and not _under(path, "/api"):
        return False  # static files, login page, metrics, anything else
    if path in PASSIVE_POST_PATHS:
        return False
    if method.upper() in MUTATING_METHODS:
        return True
    if any(_under(path, p) for p in OPERATIONAL_PREFIXES):
        return False
    return path not in PASSIVE_GET_PATHS and not PASSIVE_GET_RE.match(path)


def pick_interval_minutes(idle: bool, active_minutes: int, idle_minutes: int) -> int:
    """Minutes between scheduled pulls: the idle interval is never shorter than the active one."""
    return max(active_minutes, idle_minutes) if idle else active_minutes


class ActivityTracker:
    """Last-interaction clock. A fresh process counts as active (a deploy syncs right away)."""

    def __init__(self, idle_after_seconds: float = 12 * 3600, clock: Callable[[], float] = time.monotonic,
                 on_resume: Optional[Callable[[], None]] = None):
        self.idle_after_seconds = idle_after_seconds
        self._clock = clock
        self.on_resume = on_resume
        self.last_interaction = clock()
        self._resumed = False

    def age_seconds(self) -> float:
        return max(0.0, self._clock() - self.last_interaction)

    def is_idle(self) -> bool:
        return self.age_seconds() > self.idle_after_seconds

    def touch(self) -> bool:
        """Records an interaction; True if it ended an idle period (the caller should sync promptly)."""
        resumed = self.is_idle()
        self.last_interaction = self._clock()
        if resumed:
            self._resumed = True
            if self.on_resume:
                self.on_resume()
        return resumed

    def record(self, method: str, path: str) -> bool:
        """Counts the request if it is a user interaction. Returns True if it ended an idle period."""
        return self.touch() if is_user_interaction(method, path) else False

    def take_resumed(self) -> bool:
        """True once after an idle period ended (so the worker can pull without waiting out its interval)."""
        resumed, self._resumed = self._resumed, False
        return resumed


class ActivityMiddleware:
    """ASGI middleware: notes interactions. Mounted inside the security layer so rejected requests don't count."""

    def __init__(self, app, tracker: ActivityTracker):
        self.app = app
        self.tracker = tracker

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self.tracker.record(scope.get("method", "GET"), scope.get("path", ""))
        return await self.app(scope, receive, send)


tracker = ActivityTracker()
