"""The login part of GET /api/drivers/simkl/status, tested without the route (simkl/auth_state.py)."""

from datetime import timedelta

import pytest

from penguwatch import clock
from penguwatch.drivers.simkl import auth as auth_state

MARGIN = 86400
NOW = 1_000_000.0


def status(**kw):
    args = dict(has_token=True, stored_state=None, has_refresh_token=True, expires_at=None, now=NOW,
                renew_margin=MARGIN)
    args.update(kw)
    return auth_state.login_status(**args)


def test_no_token_is_disconnected_whatever_else_is_stored():
    assert status(has_token=False, stored_state=auth_state.NEEDS_RECONNECT) == "disconnected"


def test_a_stored_needs_reconnect_is_the_banner():
    assert status(stored_state=auth_state.NEEDS_RECONNECT) == "needs_reconnect"


@pytest.mark.parametrize("has_refresh, expires_in, expected", [
    (False, MARGIN - 1, "expiring"),   # cannot renew itself and about to lapse
    (False, MARGIN, "ok"),             # exactly the margin left: not yet
    (False, None, "ok"),               # no known expiry
    (True, 10, "ok"),                  # it renews itself
])
def test_expiring_only_when_the_login_cannot_renew_itself(has_refresh, expires_in, expected):
    expires_at = None if expires_in is None else NOW + expires_in
    assert status(has_refresh_token=has_refresh, expires_at=expires_at) == expected


def test_a_note_about_a_refused_renewal_unless_the_banner_shows():
    note = lambda **kw: auth_state.renewal_note(**{"has_token": True, "status": "ok", "stored_note": None,
                                                   "refresh_rejected": False, "expires_at": None, **kw})
    assert note() is None
    assert note(stored_note="stored") == "stored"
    assert note(refresh_rejected=True, expires_at=None) == auth_state.renewal_failed_note(None)
    assert note(stored_note="stored", status="needs_reconnect") is None
    assert note(stored_note="stored", has_token=False) is None


class _Client:
    def __init__(self, info=None, fail=False):
        self.calls, self.info, self.fail = 0, info, fail

    async def get_user_settings(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("down")
        return self.info


async def test_user_info_is_fetched_once_and_cached_for_six_hours(monkeypatch):
    now = [clock.utc_now()]
    monkeypatch.setattr(clock, "utc_now", lambda: now[0])
    client = _Client({"user": {"name": "x"}})
    info, cache = await auth_state.cached_user_info((None, None), client)
    assert info == {"user": {"name": "x"}} and client.calls == 1
    info, cache = await auth_state.cached_user_info(cache, client)
    assert client.calls == 1
    now[0] += timedelta(hours=6, seconds=1)
    await auth_state.cached_user_info(cache, client)
    assert client.calls == 2


async def test_a_failed_user_fetch_gives_none_logs_and_keeps_the_cache(caplog):
    cache = (None, None)
    with caplog.at_level("WARNING", logger="penguwatch.drivers.simkl.auth"):
        info, kept = await auth_state.cached_user_info(cache, _Client(fail=True))
    assert info is None and kept is cache
    assert "Failed to fetch SIMKL user settings: down" in caplog.text
