"""Request guards for PenguWatch: the shared homeguard guards plus this app's profile.

The guards themselves (Host allow-list, Origin/CSRF check, access token and session cookie, body caps, login
throttle, rate limits, security headers) live in the vendored `homeguard` package; edit them upstream, not here.
This module declares what is specific to PenguWatch and re-exports the names the rest of the app imports.
"""

import base64
import hashlib
import re
from pathlib import Path
from typing import Optional

from homeguard.security import (
    LOGIN_PAGE,
    AppProfile,
    CacheRule,
    FileSessionStore,
    LoginThrottle,
    SecurityMiddleware,
    host_allowed,
    origin_allowed,
    rate_limit,
    reset_rate_limits,
    session_status,
)
from homeguard.security import SecurityConfig as _BaseConfig

__all__ = ["CSP", "PROFILE", "PUBLIC_PATHS", "SESSION_COOKIE", "FileSessionStore", "LoginThrottle", "SecurityConfig",
           "SecurityMiddleware", "host_allowed", "origin_allowed", "rate_limit", "reset_rate_limits", "session_status",
           "session_store_for"]


def _login_style_hash() -> str:
    """The sign-in page (homeguard's LOGIN_PAGE) carries one <style> block of its own: it is allowed by its hash, so
    style-src needs no 'unsafe-inline'. The page and the dashboard have no other inline style (tests check it)."""
    style = re.search(r"<style>(.*?)</style>", LOGIN_PAGE.format(title="", head_extra="", error=""), re.S).group(1)
    return "'sha256-" + base64.b64encode(hashlib.sha256(style.encode()).digest()).decode() + "'"


CSP = (
    "default-src 'self'; "
    "img-src 'self' data: https:; "
    # No inline styles: the dashboard's are in stylesheets (and set as CSSOM properties from scripts)
    f"style-src 'self' {_login_style_hash()}; "
    # No inline scripts or handlers anywhere: the dashboard uses delegated data-call/data-action listeners.
    "script-src 'self'; "
    "connect-src 'self'; font-src 'self' data:; object-src 'none'; base-uri 'none'; "
    "worker-src 'self'; manifest-src 'self'; "
    "form-action 'self'; frame-ancestors 'self'"
)

# Open even when a token is configured: container health check, login page, the two installable-app files
# (manifest and service worker; they contain no user data) and the icon browsers ask for on the sign-in page. /static/ is open too (homeguard's default prefix).
PUBLIC_PATHS = ("/api/health", "/login", "/manifest.webmanifest", "/sw.js", "/favicon.ico")

CALENDAR_PURPOSE = "wt-ics-v1"

PROFILE = AppProfile(
    slug="penguwatch",
    title="PenguWatch",
    env_prefix="PENGUWATCH",
    cookie_name="wt_session",
    session_purpose="wt-session-v1",  # part of the cookie's signing key: changing it signs every browser out
    csp=CSP,
    public_paths=PUBLIC_PATHS,
    referrer_policy="same-origin",
    # The sign-in page in the dashboard's look and theme (static/login.css overrides homeguard's own style block)
    login_head_extra=('<link rel="manifest" href="/manifest.webmanifest"><script src="/static/theme.js"></script>'
                      '<link rel="stylesheet" href="/static/tokens.css"><link rel="stylesheet" href="/static/login.css">'),
    no_token_warning=(
        "No PENGUWATCH_ACCESS_TOKEN set: anyone who can reach this port can read and change your "
        "library and your tracker account link. Set a token if the port is reachable beyond this PC."
    ),
    # Calendar apps can't send headers, so the feed is also reachable with ?key=<calendar_key>.
    capabilities={"/api/calendar.ics": CALENDAR_PURPOSE},
    cache_rules=(
        # the MCP endpoint's answers are the user's data, for one caller: never stored by a cache in between
        CacheRule("/mcp", "no-store"),
        # content-addressed: the hash is part of the path, so the bytes behind it never change.
        # Errors are never cached: a 404 for an old hash must not outlive a rollback.
        CacheRule("/static/v/", "public, max-age=31536000, immutable", "no-store"),
        # unversioned module files: always revalidate (only reached by a stale or hand-typed URL)
        CacheRule("/static/js/", "no-cache"),
        # asset URLs carry ?v=<content hash>, so a change in content changes the URL
        CacheRule("/static/", "public, max-age=86400"),
    ),
)

SESSION_COOKIE = PROFILE.cookie_name


def session_store_for(db_path: str) -> Optional[FileSessionStore]:
    """Where sign-outs (POST /logout, and "sign out everywhere") are kept: sessions.json next to the database, in the
    data directory. None for an in-memory database (tests): there is no data directory, so a sign-out only clears
    the browser's cookie and "sign out everywhere" is off."""
    if db_path == ":memory:" or db_path.startswith("file::memory:") or "mode=memory" in db_path:
        return None
    return FileSessionStore(Path(db_path).parent / "sessions.json")


class SecurityConfig(_BaseConfig):
    PROFILE = PROFILE

    @property
    def calendar_key(self) -> str:
        return self.capability_key(CALENDAR_PURPOSE)
