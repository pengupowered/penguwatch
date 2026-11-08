"""Persisted SIMKL login state: token expiry, last renewal, and the "needs reconnect" flag.

Two levels, so the UI only asks for a manual reconnect when one is really needed:
  * needs_reconnect (KEY_STATE): the login is not working and cannot renew itself -> app-wide banner.
  * renewal failed (KEY_SOFT): SIMKL refused the refresh token but the access token still works -> a note on
    Services & Sync, no banner. It becomes needs_reconnect only when an API call then really gets a 401.

Kept separate from client.py (which must not know about the repository) and importing nothing
from it, so there is no import cycle."""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from penguwatch import clock

logger = logging.getLogger("penguwatch.drivers.simkl.auth")

KEY_EXPIRES = "driver.simkl.token_expires_at"   # ISO UTC; absent = unknown / never expires
KEY_RENEWED = "driver.simkl.token_renewed_at"   # ISO UTC of the last (re)login or automatic renewal
KEY_STATE = "driver.simkl.auth_state"           # only ever "needs_reconnect"; absent = ok
KEY_MSG = "driver.simkl.auth_message"
KEY_SOFT = "driver.simkl.renewal_note"          # renewal refused while the access token still works; absent = fine

NEEDS_RECONNECT = "needs_reconnect"
RECONNECT_MSG = "Your SIMKL login could not be renewed automatically. Click 'Reconnect SIMKL' to log in again."
UNREACHABLE_MSG = "SIMKL is unreachable right now; will retry automatically."
RETRY_MSG = "SIMKL did not accept the login just now; will retry automatically."


def iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def ts_from_iso(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def renewal_failed_note(expires_at: Optional[float]) -> str:
    if expires_at and expires_at > clock.utc_now().timestamp():
        when = datetime.fromtimestamp(expires_at, clock.tz()).strftime("%b %d, %Y").replace(" 0", " ")
        return f"Automatic renewal failed; reconnect before the login expires on {when}."
    return "Automatic renewal failed; reconnect SIMKL soon."


async def clear_needs_reconnect(repo) -> None:
    """A good new login: no banner, no note."""
    await repo.set_states({KEY_STATE: None, KEY_MSG: None, KEY_SOFT: None})


async def mark_needs_reconnect(repo, message: str = RECONNECT_MSG) -> None:
    await repo.set_states({KEY_STATE: NEEDS_RECONNECT, KEY_MSG: message, KEY_SOFT: None, "driver.simkl.last_error": message})


async def mark_renewal_failed(repo, expires_at: Optional[float], still_current=None) -> None:
    """SIMKL refused the refresh token. The access token may well keep working until it expires, so this is only
    a note; the banner follows if (and only if) an API call then returns 401.

    `still_current` (a callable) says whether the refused login is still the installed one; it is checked right
    before the write, so a new login that landed meanwhile (and cleared the note) is not overwritten."""
    if await repo.get_state(KEY_STATE) == NEEDS_RECONNECT:
        return  # already the stronger state
    if still_current is not None and not still_current():
        return  # the login this refusal was about has been replaced
    await repo.set_state(KEY_SOFT, renewal_failed_note(expires_at))


async def token_works(repo, client) -> None:
    """An authenticated sync just succeeded, so the access token works: that settles the banner. If the refresh
    token is still known to be refused, the banner steps down to the note; otherwise a stale note is dropped
    (it comes back at the next failed renewal)."""
    if not (await repo.get_state(KEY_STATE) or await repo.get_state(KEY_MSG) or await repo.get_state(KEY_SOFT)):
        return
    note = renewal_failed_note(client.token_expires_at) if client.refresh_rejected else None
    await repo.set_states({KEY_STATE: None, KEY_MSG: None, KEY_SOFT: note})


async def record_auth_failure(repo, client, reconnect_msg: str) -> str:
    """Handles a SimklAuthError (a 401 that survived the client's own renewal attempt); returns the message to show.

    Only a login that cannot renew itself needs the user: the refresh token was refused, or there never was one
    (pasted token). Otherwise renewal is still in progress (the token endpoint was unreachable, so the request went
    out with the expired token): record why in last_error and let the next attempt retry, with no banner."""
    if client.refresh_rejected or not client.refresh_token:
        await mark_needs_reconnect(repo, reconnect_msg)
        return reconnect_msg
    msg = UNREACHABLE_MSG if client.last_refresh_outcome == "transient" else RETRY_MSG
    await repo.set_state("driver.simkl.last_error", msg)
    return msg


async def persist_tokens(repo, access: str, refresh: Optional[str], expires_at: Optional[float],
                         *, new_login: bool = False) -> None:
    """Stores a fresh login or renewal; a good token also clears any pending reconnect request.

    A renewal without a refresh token keeps the stored one. A new login (`new_login=True`) is authoritative: it
    replaces the stored refresh token and expiry with exactly what the login returned, so a login that carries no
    refresh token (the classic PIN flow) deletes the old one instead of pairing itself with it.

    The access token, refresh token and expiry go into the database in ONE transaction: refresh tokens may be
    single-use, so a crash between separate writes could leave a new access token next to a dead refresh token.
    A failed write is retried once; if that fails too the error is logged at CRITICAL and re-raised."""
    values: Dict[str, Optional[str]] = {
        "driver.simkl.access_token": access,
        KEY_EXPIRES: iso_from_ts(expires_at) if expires_at else None,
        KEY_RENEWED: clock.utc_now().isoformat(),
        KEY_STATE: None,
        KEY_MSG: None,
        KEY_SOFT: None,
    }
    if refresh:
        values["driver.simkl.refresh_token"] = refresh
    elif new_login:
        values["driver.simkl.refresh_token"] = None  # set_states deletes the key
    for attempt in (1, 2):
        try:
            await repo.set_states(values)
            return
        except Exception as e:
            if attempt == 1:
                logger.warning(f"Could not persist the SIMKL tokens, retrying once: {e}")
                continue
            logger.critical(
                "Could not persist the SIMKL tokens after a retry; the stored login may be out of date and the "
                f"next restart could require reconnecting SIMKL: {e}", exc_info=True)
            raise


async def load_expiry(repo) -> Optional[float]:
    return ts_from_iso(await repo.get_state(KEY_EXPIRES))


async def read(repo) -> Dict[str, Any]:
    return {
        "auth_state_raw": await repo.get_state(KEY_STATE),
        "renewal_note": await repo.get_state(KEY_SOFT),
        "auth_message": await repo.get_state(KEY_MSG),
        "token_expires_at": await repo.get_state(KEY_EXPIRES),
        "token_renewed_at": await repo.get_state(KEY_RENEWED),
    }


# --- what GET /api/simkl/status tells the page about the login ----------------------------------------------------

USER_INFO_TTL = timedelta(hours=6)
FAILED_USER_INFO_TTL = timedelta(minutes=5)  # a failed fetch is not retried on every poll of the status


def login_status(*, has_token: bool, stored_state: Optional[str], has_refresh_token: bool,
                 expires_at: Optional[float], now: float, renew_margin: float) -> str:
    """"disconnected", "needs_reconnect" (the banner: only once the login really stopped working), "expiring" (a
    login that cannot renew itself is about to lapse) or "ok"."""
    if not has_token:
        return "disconnected"
    if stored_state == NEEDS_RECONNECT:
        return "needs_reconnect"
    if not has_refresh_token and expires_at is not None and expires_at - now < renew_margin:
        return "expiring"
    return "ok"


def renewal_note(*, has_token: bool, status: str, stored_note: Optional[str], refresh_rejected: bool,
                 expires_at: Optional[float]) -> Optional[str]:
    """SIMKL refused the renewal but the token still works: a note, not the banner (so none while the banner shows)."""
    if not has_token or status == "needs_reconnect":
        return None
    return stored_note or (renewal_failed_note(expires_at) if refresh_rejected else None)


async def describe(repo, client, renew_margin: float) -> Dict[str, Any]:
    """The login fields of the status response, from the stored state and the client's tokens."""
    stored = await read(repo)
    expires_at = client.token_expires_at
    has_token = bool(client.access_token)
    status = login_status(has_token=has_token, stored_state=stored["auth_state_raw"],
                          has_refresh_token=bool(client.refresh_token), expires_at=expires_at, now=time.time(),
                          renew_margin=renew_margin)
    return {
        "auth_state": status,
        "auth_message": stored["auth_message"],
        "renewal_note": renewal_note(has_token=has_token, status=status, stored_note=stored["renewal_note"],
                                     refresh_rejected=client.refresh_rejected, expires_at=expires_at),
        "token_expires_at": iso_from_ts(expires_at) if expires_at else None,
        "token_renewed_at": stored["token_renewed_at"],
        "can_auto_renew": bool(client.refresh_token) and not client.refresh_rejected,
    }


async def cached_user_info(cache: Tuple[Optional[datetime], Any], client) -> Tuple[Any, Tuple[Optional[datetime], Any]]:
    """(the SIMKL user settings, the cache to keep). The page polls the status, so SIMKL is asked at most every
    USER_INFO_TTL; a failed fetch gives None. With nothing cached the failure itself is remembered for
    FAILED_USER_INFO_TTL (so a page that polls does not call SIMKL again and again while it is unreachable); an older
    good answer is kept as it was."""
    cached_at, cached = cache
    age = clock.utc_now() - cached_at if cached_at else None
    if cached is not None and age is not None and age < USER_INFO_TTL:
        return cached, cache
    if cached is None and age is not None and age < FAILED_USER_INFO_TTL:
        return None, cache
    try:
        info = await client.get_user_settings()
    except Exception as e:
        logger.warning(f"Failed to fetch SIMKL user settings: {e}")
        return None, ((clock.utc_now(), None) if cached is None else cache)
    return info, (clock.utc_now(), info)
