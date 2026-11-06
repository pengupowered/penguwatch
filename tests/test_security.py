"""PenguWatch's security wiring: the guards themselves are tested in homeguard (tests/test_security.py there);
these check that they are mounted on the real app with PenguWatch's profile (cookie, CSP, calendar key, quotas)."""

import hashlib
import hmac
import json
import os
import stat
import time

import pytest
from httpx import AsyncClient, ASGITransport

from penguwatch.main import app, security_config
from penguwatch.security import FileSessionStore, SecurityConfig, session_store_for
from tests.test_api import setup_test_app  # noqa: F401  (fixture)


@pytest.fixture
def token_mode(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "s3cret-token")
    return security_config


def client(**kw):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", **kw)


# --- CSRF / DNS rebinding --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cross_origin_post_is_blocked(setup_test_app):
    async with client() as ac:
        blocked = await ac.post("/api/sync/review/mode", json={"enabled": False}, headers={"Origin": "http://evil.example"})
        assert blocked.status_code == 403
        # the change must not have happened
        assert (await ac.get("/api/sync/review")).json()["review_mode"] is True

        same = await ac.post("/api/sync/review/mode", json={"enabled": False}, headers={"Origin": "http://test"})
        assert same.status_code == 200
        no_origin = await ac.post("/api/sync/review/mode", json={"enabled": True})  # curl, scripts
        assert no_origin.status_code == 200


@pytest.mark.asyncio
async def test_unexpected_host_is_rejected(setup_test_app):
    async with client() as ac:
        res = await ac.get("/api/stats", headers={"Host": "evil.example.com"})
        assert res.status_code == 421


# --- access token ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_token_required_when_configured(setup_test_app, token_mode):
    async with client() as ac:
        assert (await ac.get("/api/stats")).status_code == 401
        assert (await ac.get("/api/health")).status_code == 200  # container health check stays open
        ok = await ac.get("/api/stats", headers={"Authorization": "Bearer s3cret-token"})
        assert ok.status_code == 200
        bad = await ac.get("/api/stats", headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401
        page = await ac.get("/", headers={"Accept": "text/html"})
        assert page.status_code == 303 and page.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_login_sets_httponly_session_cookie(setup_test_app, token_mode):
    async with client() as ac:
        wrong = await ac.post("/login", data={"token": "wrong"})
        assert wrong.status_code == 401
        res = await ac.post("/login", data={"token": "s3cret-token"})
        assert res.status_code == 303
        cookie = res.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
        value = cookie.split(";")[0].split("=", 1)[1]
        authed = await ac.get("/api/stats", headers={"Cookie": f"wt_session={value}"})
        assert authed.status_code == 200
        # the cookie is not the token itself
        assert value != "s3cret-token"


@pytest.mark.asyncio
async def test_calendar_feed_uses_capability_key(setup_test_app, token_mode):
    async with client() as ac:
        assert (await ac.get("/api/calendar.ics")).status_code == 401
        ok = await ac.get(f"/api/calendar.ics?key={token_mode.calendar_key}")
        assert ok.status_code == 200 and "BEGIN:VCALENDAR" in ok.text
        assert (await ac.get("/api/calendar.ics?key=guess")).status_code == 401


# --- limits & headers ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_body_size_cap(setup_test_app):
    async with client() as ac:
        res = await ac.post("/api/sync/review/mode", content=b"x" * (300 * 1024),
                            headers={"Content-Type": "application/json"})
        assert res.status_code == 413


@pytest.mark.asyncio
async def test_security_headers_present(setup_test_app):
    async with client() as ac:
        res = await ac.get("/api/stats")
        assert res.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'self'" in res.headers["content-security-policy"]
        assert "object-src 'none'" in res.headers["content-security-policy"]
        assert res.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_quota_burning_endpoints_are_rate_limited(setup_test_app):
    from penguwatch.security import reset_rate_limits

    reset_rate_limits()
    async with client() as ac:
        codes = [(await ac.get("/api/drivers/simkl/diagnose")).status_code for _ in range(8)]
    # SIMKL is unreachable in tests (the probe records the failure) but the limiter must cut in at 6 per minute
    assert codes.count(429) == 2 and all(c != 429 for c in codes[:6])


@pytest.mark.asyncio
async def test_csp_forbids_inline_scripts(setup_test_app):
    async with client() as ac:
        csp = (await ac.get("/api/stats")).headers["content-security-policy"]
        page = await ac.get("/")
    script_src = next(p for p in csp.split(";") if p.strip().startswith("script-src"))
    assert "'unsafe-inline'" not in script_src and "'self'" in script_src
    # and the shipped UI really has none left (template and script)
    import re

    pattern = r'\son(click|change|error|input|submit|load)="'
    assert not re.search(pattern, page.text)
    async with client() as ac:
        js = (await ac.get("/static/app.js")).text
    assert not re.search(pattern, js)


# --- profile: values existing sign-ins and calendar links depend on ----------------------------

def test_calendar_derivation_is_unchanged_and_the_old_session_cookie_is_refused():
    """Calendar URLs issued before keep working (same HMAC of the same purpose). The session cookie from before signed
    sessions (a fixed HMAC of the token) no longer signs anyone in: people sign in once more."""
    cfg = SecurityConfig(access_token="tok")
    assert cfg.calendar_key == hmac.new(b"tok", b"wt-ics-v1", hashlib.sha256).hexdigest()[:32]
    assert cfg.session_valid(cfg.issue_session())
    assert not cfg.session_valid(hmac.new(b"tok", b"wt-session-v1", hashlib.sha256).hexdigest())


# --- sign out ----------------------------------------------------------------------------------

@pytest.fixture
def sessions(token_mode, monkeypatch, tmp_path):
    store = FileSessionStore(tmp_path / "data" / "sessions.json")
    monkeypatch.setattr(security_config, "session_store", store)
    return store


def _signed_in(seconds_ago: float = 1.0) -> str:
    """A cookie the app issued `seconds_ago` (made directly: the login throttle is shared by every test here)."""
    return security_config.issue_session(now_ms=int((time.time() - seconds_ago) * 1000))


def _cookie(value: str) -> dict:
    return {"Cookie": f"wt_session={value}"}


@pytest.mark.asyncio
async def test_session_endpoint_tells_the_page_whether_to_offer_sign_out(setup_test_app, sessions, monkeypatch):
    async with client() as ac:
        assert (await ac.get("/api/session", headers=_cookie(_signed_in()))).json() == {
            "sign_out": True, "sign_out_everywhere": True}
        by_bearer = await ac.get("/api/session", headers={"Authorization": "Bearer s3cret-token"})
        assert by_bearer.json() == {"sign_out": False, "sign_out_everywhere": False}
        assert (await ac.get("/api/session")).status_code == 401
        monkeypatch.setattr(security_config, "session_store", None)
        assert (await ac.get("/api/session", headers=_cookie(_signed_in()))).json() == {
            "sign_out": True, "sign_out_everywhere": False}
        monkeypatch.setattr(security_config, "access_token", "")
        assert (await ac.get("/api/session")).json() == {"sign_out": False, "sign_out_everywhere": False}


@pytest.mark.asyncio
async def test_sign_out_and_sign_out_everywhere(setup_test_app, sessions):
    async with client() as ac:
        mine, other = _signed_in(2), _signed_in(1)
        out = await ac.post("/logout", headers={"Origin": "http://test", **_cookie(mine)})
        assert out.status_code == 303 and out.headers["location"] == "/login" and "Max-Age=0" in out.headers["set-cookie"]
        assert (await ac.get("/api/stats", headers=_cookie(mine))).status_code == 401  # a copy of it is refused too
        assert (await ac.get("/api/stats", headers=_cookie(other))).status_code == 200
        everywhere = await ac.post("/logout?everywhere=1", headers={"Origin": "http://test", **_cookie(other)})
        assert everywhere.status_code == 303
        assert (await ac.get("/api/stats", headers=_cookie(other))).status_code == 401
        assert (await ac.get("/api/stats", headers={"Authorization": "Bearer s3cret-token"})).status_code == 200
        assert (await ac.get(f"/api/calendar.ics?key={security_config.calendar_key}")).status_code == 200
        assert json.loads(sessions.path.read_text())["not_before_ms"] > 0
        assert stat.S_IMODE(os.stat(sessions.path).st_mode) == 0o600
        cross = await ac.post("/logout", headers={"Origin": "http://evil.example", **_cookie(_signed_in())})
        assert cross.status_code == 403


def test_sign_outs_are_kept_next_to_the_database(tmp_path):
    store = session_store_for(str(tmp_path / "data" / "penguwatch.db"))
    assert isinstance(store, FileSessionStore) and store.path == tmp_path / "data" / "sessions.json"
    for memory in (":memory:", "file::memory:?cache=shared", "file:x?mode=memory"):
        assert session_store_for(memory) is None


@pytest.mark.asyncio
async def test_startup_gives_the_guards_the_session_store(tmp_path, monkeypatch):
    for var in ("SIMKL_ACCESS_TOKEN", "SIMKL_CLIENT_ID", "TMDB_API_KEY", "PENGUWATCH_NOTIFY_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PENGUWATCH_DB_PATH", str(tmp_path / "data" / "app.db"))
    monkeypatch.setattr(security_config, "session_store", None)
    try:
        async with app.router.lifespan_context(app):
            assert security_config.session_store.path == tmp_path / "data" / "sessions.json"
    finally:
        if hasattr(app.state, "container"):
            del app.state.container


def test_env_prefix_is_penguwatch(monkeypatch):
    monkeypatch.setenv("PENGUWATCH_ACCESS_TOKEN", "abc")
    monkeypatch.setenv("PENGUWATCH_ALLOWED_HOSTS", "Watch.Example.com")
    cfg = SecurityConfig.from_env()
    assert cfg.access_token == "abc" and cfg.allowed_hosts == ["watch.example.com"]


@pytest.mark.asyncio
async def test_sign_in_page_and_rejections_carry_the_security_headers(setup_test_app, token_mode):
    async with client() as ac:
        responses = [await ac.get("/login"), await ac.get("/api/stats"),
                     await ac.get("/api/stats", headers={"Host": "evil.example.com"})]
    assert [r.status_code for r in responses] == [200, 401, 421]
    for res in responses:
        assert "frame-ancestors 'self'" in res.headers["content-security-policy"]
        assert res.headers["x-frame-options"] == "SAMEORIGIN" and res.headers["referrer-policy"] == "same-origin"
        assert res.headers.get_list("cache-control") == ["no-store"]
        assert "x-xss-protection" not in res.headers
    login = responses[0].text
    assert "PenguWatch - sign in" in login and 'rel="manifest" href="/manifest.webmanifest"' in login


@pytest.mark.asyncio
async def test_non_ascii_credentials_are_unauthorized_not_an_error(setup_test_app, token_mode):
    async with client() as ac:
        res = await ac.get("/api/stats", headers={"Authorization": "Bearer caf\u00e9".encode("latin-1")})
        assert res.status_code == 401
        assert (await ac.get("/api/calendar.ics?key=caf%C3%A9")).status_code == 401


@pytest.mark.asyncio
async def test_favicon_is_public_so_the_sign_in_page_asks_without_an_error(setup_test_app, token_mode):
    async with client() as ac:
        res = await ac.get("/favicon.ico")
    assert res.status_code == 200 and res.headers["content-type"] == "image/x-icon" and res.content[:4] == b"\x00\x00\x01\x00"


@pytest.mark.asyncio
async def test_the_page_leaves_for_the_sign_in_page_on_homeguards_refusal(setup_test_app, token_mode):
    """api.js goes to /login on a 401 that carries homeguard's challenge, and only on that one."""
    import re
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "src/penguwatch/web/static/js/api.js").read_text(encoding="utf-8")
    assert 'res.status === 401 && /^Bearer realm=/i.test(res.headers.get("www-authenticate") || "")' in js
    assert 'window.location.assign("/login")' in js and "if (signInRefused(res)) sessionEnded();" in js
    assert "if (leaving) return;" in js  # once
    async with client() as ac:
        refused = await ac.get("/api/stats", headers={"Cookie": f"wt_session={security_config.issue_session(now_ms=1)}"})
    assert refused.status_code == 401 and re.match(r"Bearer realm=", refused.headers["www-authenticate"], re.I)
