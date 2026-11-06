"""SIMKL token renewal: single-flight refresh, proactive renewal, and the persisted auth state."""

import asyncio
import inspect
import logging
import time
from contextlib import asynccontextmanager

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from tests.test_two_way_sync import mock_reads
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import ServiceConfig
from penguwatch.main import app, sync_worker, worker_step
from penguwatch.drivers.simkl import auth as auth_state
from penguwatch.drivers.simkl.client import SIMKL_BASE_URL, SimklAuthError, SimklClient
from tests.simkl_helpers import simkl_engine
from penguwatch.drivers.base import SyncOp

TOKEN_URL = f"{SIMKL_BASE_URL}/oauth2/token"
ACTIVITIES_URL = f"{SIMKL_BASE_URL}/sync/activities"


def wire(client: SimklClient, repo: DatabaseRepository) -> None:
    async def persisted(access, refresh, expires_at):
        await auth_state.persist_tokens(repo, access, refresh, expires_at)

    async def rejected(expires_at, still_current=None):
        await auth_state.mark_renewal_failed(repo, expires_at, still_current)

    client.on_token_refreshed = persisted
    client.on_auth_rejected = rejected


@pytest.fixture
async def env(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "renew.db"))
    await repo.initialize([ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99, aliases=["Apple TV+"])])
    client = SimklClient("cid", "tok")
    client.retry_base_delay = 0
    wire(client, repo)
    sync = simkl_engine(client, repo)
    await repo.set_state("sync_review_mode", "off")
    yield repo, client, sync
    await client.aclose()
    await repo.close()


@pytest.mark.asyncio
@respx.mock
async def test_concurrent_401s_refresh_once(env):
    repo, client, _ = env
    client.set_tokens("old", "r1", None)

    async def activities(request):
        await asyncio.sleep(0.01)  # yield so all eight requests are genuinely in flight together
        if request.headers["authorization"] == "Bearer old":
            return httpx.Response(401, json={"error": "user_token_failed"})
        return httpx.Response(200, json={"all": None})

    async def token(request):
        await asyncio.sleep(0.02)
        return httpx.Response(200, json={"access_token": "new", "refresh_token": "r2", "expires_in": 604800})

    respx.get(ACTIVITIES_URL).mock(side_effect=activities)
    token_route = respx.post(TOKEN_URL).mock(side_effect=token)

    results = await asyncio.gather(*[client.get_activities() for _ in range(8)])
    assert results == [{"all": None}] * 8
    assert token_route.call_count == 1
    assert client.refresh_token == "r2" and client.access_token == "new"
    assert await repo.get_state("driver.simkl.refresh_token") == "r2"


@pytest.mark.asyncio
@respx.mock
async def test_non_expired_401_refreshes_once_then_raises(env):
    _, client, _ = env
    client.set_tokens("tok", "r1", None)
    respx.get(ACTIVITIES_URL).respond(status_code=401, json={"error": "user_token_failed"})
    token_route = respx.post(TOKEN_URL).respond(json={"access_token": "new", "refresh_token": "r2"})
    with pytest.raises(SimklAuthError):
        await client.get_activities()
    assert token_route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_refresh_records_expiry_from_expires_in(env, monkeypatch):
    repo, client, _ = env
    monkeypatch.setattr("penguwatch.drivers.simkl.client.time.time", lambda: 1000.0)
    seen = {}
    inner = client.on_token_refreshed

    async def spy(a, r, e):
        seen["expires_at"] = e
        await inner(a, r, e)

    client.on_token_refreshed = spy
    client.set_tokens("tok", "r1", None)
    respx.post(TOKEN_URL).respond(json={"access_token": "new", "refresh_token": "r2", "expires_in": 3600})
    assert await client.refresh_access_token() is True
    assert client.token_expires_at == 4600.0 and seen["expires_at"] == 4600.0
    assert await auth_state.load_expiry(repo) == 4600.0
    assert await repo.get_state(auth_state.KEY_RENEWED)


@pytest.mark.asyncio
@respx.mock
async def test_refresh_records_expiry_defaults_to_7_days(env, monkeypatch):
    repo, client, _ = env
    monkeypatch.setattr("penguwatch.drivers.simkl.client.time.time", lambda: 1000.0)
    client.set_tokens("tok", "r1", None)
    respx.post(TOKEN_URL).respond(json={"access_token": "new", "refresh_token": "r2"})
    assert await client.refresh_access_token() is True
    assert client.token_expires_at == 1000.0 + 7 * 86400
    assert await auth_state.load_expiry(repo) == 1000.0 + 7 * 86400


@pytest.mark.asyncio
@respx.mock
async def test_rejected_refresh_is_a_note_until_a_call_really_fails(env):
    repo, client, sync = env
    client.set_tokens("tok", "r1", None)
    respx.get(ACTIVITIES_URL).respond(status_code=401, json={"error": "user_token_failed"})
    token_route = respx.post(TOKEN_URL).respond(status_code=400, json={"error": "invalid_grant"})
    with pytest.raises(SimklAuthError):
        await client.get_activities()
    assert client.refresh_rejected and client.last_refresh_outcome == "rejected"
    assert token_route.call_count == 1
    # renewal failed, but nothing has stopped working yet: a note, no banner
    assert await repo.get_state(auth_state.KEY_STATE) is None
    assert "Automatic renewal failed" in await repo.get_state(auth_state.KEY_SOFT)

    with pytest.raises(SimklAuthError):
        await client.get_activities()
    assert token_route.call_count == 1  # no hammering a refresh token SIMKL already refused

    # the sync that hits the 401 with no working renewal is what raises the banner
    result = await sync.sync()
    assert result["status"] == "error"
    assert "Reconnect SIMKL" in result["error"]
    assert await repo.get_state(auth_state.KEY_STATE) == auth_state.NEEDS_RECONNECT
    assert "Reconnect SIMKL" in await repo.get_state(auth_state.KEY_MSG)
    assert await repo.get_state(auth_state.KEY_SOFT) is None  # the banner replaces the note
    assert token_route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_rejected_renewal_note_names_the_expiry_date(env):
    repo, client, _ = env
    expires = time.time() + 5 * 86400
    client.set_tokens("tok", "r1", expires)
    respx.post(TOKEN_URL).respond(status_code=403, json={"error": "invalid_grant"})
    assert await client.refresh_access_token() is False
    note = await repo.get_state(auth_state.KEY_SOFT)
    assert note.startswith("Automatic renewal failed; reconnect before the login expires on ")
    assert await repo.get_state(auth_state.KEY_STATE) is None


@pytest.mark.asyncio
@respx.mock
async def test_transient_refresh_failure_is_not_rejection(env):
    repo, client, _ = env
    client.set_tokens("tok", "r1", None)
    token_route = respx.post(TOKEN_URL).respond(status_code=503)
    assert await client.renew_if_due() == "failed"
    assert not client.refresh_rejected and client.last_refresh_outcome == "transient"
    assert await repo.get_state(auth_state.KEY_STATE) is None
    assert await client.renew_if_due() == "throttled"
    assert token_route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_renew_if_due_matrix(env, monkeypatch):
    _, client, _ = env
    monkeypatch.setattr("penguwatch.drivers.simkl.client.time.time", lambda: 1000.0)
    token_route = respx.post(TOKEN_URL).respond(json={"access_token": "new", "refresh_token": "r2", "expires_in": 604800})

    client.set_tokens("tok", "r1", 1000.0 + 2 * 3600)
    assert await client.renew_if_due() == "renewed"
    assert token_route.call_count == 1

    client.set_tokens("tok", "r1", 1000.0 + 3 * 86400)
    assert await client.renew_if_due() == "not_needed"
    assert token_route.call_count == 1

    client.set_tokens("tok", "r1", None)
    assert await client.renew_if_due() == "renewed"
    assert token_route.call_count == 2

    client.refresh_token = None
    assert await client.renew_if_due() == "no_refresh_token"
    assert token_route.call_count == 2


@pytest.mark.asyncio
async def test_set_tokens_resets_rejection(env):
    _, client, _ = env
    client.refresh_rejected = True
    client._next_renew_try = 10 ** 12
    client.set_tokens("a", "b", 5.0)
    assert not client.refresh_rejected and client._next_renew_try == 0.0
    assert (client.access_token, client.refresh_token, client.token_expires_at) == ("a", "b", 5.0)
    client.set_tokens("c")  # a login is authoritative: no refresh token given means none
    assert client.refresh_token is None and client.token_expires_at is None


@pytest.mark.asyncio
@respx.mock
async def test_successful_sync_clears_needs_reconnect(env):
    repo, _, sync = env
    await auth_state.mark_needs_reconnect(repo)
    mock_reads()
    assert (await sync.sync(deep=False))["status"] == "success"
    assert await repo.get_state(auth_state.KEY_STATE) is None
    assert await repo.get_state(auth_state.KEY_MSG) is None


@pytest.mark.asyncio
@respx.mock
async def test_pin_login_stores_expiry_and_clears_reconnect(container, monkeypatch):
    c = container

    async def no_sync(*a, **k):
        return {}

    monkeypatch.setattr(c.sync, "sync", no_sync)
    await auth_state.mark_needs_reconnect(c.repository)
    c.drivers.get("simkl").client.refresh_rejected = True
    respx.post(TOKEN_URL).respond(json={"access_token": "newtok", "refresh_token": "ref", "expires_in": 7200,
                                        "scope": "media:read media:write"})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post("/api/drivers/simkl/connect", json={"state": {"device_code": "dc"}})
    assert res.json()["kind"] == "done"
    repo = c.repository
    assert await repo.get_state("driver.simkl.access_token") == "newtok"
    assert await repo.get_state("driver.simkl.refresh_token") == "ref"
    assert await auth_state.load_expiry(repo) is not None
    assert await repo.get_state(auth_state.KEY_RENEWED)
    assert await repo.get_state(auth_state.KEY_STATE) is None
    assert c.drivers.get("simkl").client.access_token == "newtok" and not c.drivers.get("simkl").client.refresh_rejected
    assert c.drivers.get("simkl").client.token_expires_at is not None


@pytest.mark.asyncio
async def test_disconnect_clears_auth_keys(container):
    c = container
    repo = c.repository
    await auth_state.persist_tokens(repo, "a", "r", 12345.0)
    await auth_state.mark_needs_reconnect(repo)
    c.drivers.get("simkl").client.set_tokens("a", "r", 12345.0)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.post("/api/drivers/simkl/disconnect")).json()["status"] == "disconnected"
    for key in ("driver.simkl.access_token", "driver.simkl.refresh_token", auth_state.KEY_EXPIRES, auth_state.KEY_RENEWED,
                auth_state.KEY_STATE, auth_state.KEY_MSG):
        assert await repo.get_state(key) is None
    assert c.drivers.get("simkl").client.access_token is None and c.drivers.get("simkl").client.refresh_token is None
    assert c.drivers.get("simkl").client.token_expires_at is None


def test_worker_calls_renew_if_due():
    """The worker runs the library driver's housekeeping before each step; SIMKL's is renewing the login."""
    from penguwatch.drivers.simkl.service import SimklLibrary
    assert "library.maintain()" in inspect.getsource(sync_worker)
    assert "renew_if_due" in inspect.getsource(SimklLibrary.maintain)


@pytest.mark.asyncio
@respx.mock
async def test_transient_token_endpoint_failure_is_throttled(env):
    _, client, _ = env
    client.set_tokens("old", "r1", time.time() - 10)  # expired: every request wants a pre-emptive refresh
    token_route = respx.post(TOKEN_URL).respond(status_code=503)
    act = respx.get(ACTIVITIES_URL).respond(json={"all": None})
    for _ in range(3):
        assert await client.get_activities() == {"all": None}
    assert token_route.call_count == 1  # not once per request while SIMKL's token endpoint is down
    assert act.call_count == 3
    assert all(c.request.headers["authorization"] == "Bearer old" for c in act.calls)
    assert not client.refresh_rejected and client.last_refresh_outcome == "transient"

    client._refresh_backoff_until = 0.0  # backoff over: the next request tries again
    await client.get_activities()
    assert token_route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_refresh_backoff_cleared_on_success_and_new_login(env):
    _, client, _ = env
    client.set_tokens("old", "r1", None)
    respx.post(TOKEN_URL).respond(status_code=503)
    assert await client.refresh_access_token() is False
    assert client._refresh_backoff_until > time.monotonic()
    client.set_tokens("old2", "r2", None)
    assert client._refresh_backoff_until == 0.0

    respx.post(TOKEN_URL).respond(json={"access_token": "new", "refresh_token": "r3", "expires_in": 3600})
    client._refresh_backoff_until = 0.0
    assert await client.refresh_access_token() is True
    assert client._refresh_backoff_until == 0.0


@pytest.mark.asyncio
@respx.mock
async def test_expired_token_refreshes_before_the_request(env):
    _, client, _ = env
    client.set_tokens("old", "r1", time.time() - 10)
    token_route = respx.post(TOKEN_URL).respond(json={"access_token": "fresh", "refresh_token": "r2", "expires_in": 3600})
    act = respx.get(ACTIVITIES_URL).respond(json={"all": None})
    await client.get_activities()
    assert token_route.call_count == 1 and act.call_count == 1  # no 401 round-trip
    assert act.calls[0].request.headers["authorization"] == "Bearer fresh"
    assert client.access_token == "fresh" and client.token_expires_at > time.time()


@pytest.mark.asyncio
async def test_manual_token_discards_old_refresh_token(container):
    c = container
    repo = c.repository
    await auth_state.persist_tokens(repo, "oldacc", "oldref", time.time() + 100)
    c.drivers.get("simkl").client.set_tokens("oldacc", "oldref", time.time() + 100)
    await auth_state.mark_needs_reconnect(repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post("/api/drivers/simkl/credentials", json={"client_id": "", "access_token": "pasted"})
    assert res.json()["token_set"] is True
    assert c.drivers.get("simkl").client.access_token == "pasted" and c.drivers.get("simkl").client.refresh_token is None
    assert await repo.get_state("driver.simkl.refresh_token") is None
    assert await repo.get_state(auth_state.KEY_STATE) is None
    assert await c.drivers.get("simkl").client.renew_if_due() == "no_refresh_token"  # the old refresh token can't replace it


@pytest.mark.asyncio
async def test_credentials_without_token_keeps_refresh_token(container):
    c = container
    repo = c.repository
    await repo.set_state("driver.simkl.refresh_token", "keepme")
    c.drivers.get("simkl").client.set_tokens("acc", "keepme", None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        await ac.post("/api/drivers/simkl/credentials", json={"client_id": "newcid", "access_token": ""})
    assert c.drivers.get("simkl").client.refresh_token == "keepme"
    assert await repo.get_state("driver.simkl.refresh_token") == "keepme"


@pytest.mark.asyncio
@respx.mock
async def test_successful_sync_steps_banner_down_to_note_while_renewal_is_rejected(env):
    repo, client, sync = env
    client.set_tokens("tok", "r1", None)
    client.refresh_rejected = True
    await auth_state.mark_needs_reconnect(repo)
    mock_reads()
    assert (await sync.sync(deep=False))["status"] == "success"
    # the token works again, so no banner; renewal is still refused, so the note stays
    assert await repo.get_state(auth_state.KEY_STATE) is None
    assert await repo.get_state(auth_state.KEY_MSG) is None
    assert "Automatic renewal failed" in await repo.get_state(auth_state.KEY_SOFT)


@pytest.mark.asyncio
@respx.mock
async def test_successful_sync_clears_stale_note_when_renewal_is_not_known_to_be_refused(env):
    repo, client, sync = env
    client.set_tokens("tok", "r1", None)  # e.g. after a restart: the old rejection is only remembered in the database
    await auth_state.mark_renewal_failed(repo, None)
    mock_reads()
    assert (await sync.sync(deep=False))["status"] == "success"
    assert await repo.get_state(auth_state.KEY_SOFT) is None
    assert await repo.get_state(auth_state.KEY_STATE) is None


@pytest.mark.asyncio
@respx.mock
async def test_successful_renewal_and_new_login_clear_the_note(env):
    repo, client, _ = env
    await auth_state.mark_renewal_failed(repo, None)
    client.set_tokens("tok", "r1", None)
    respx.post(TOKEN_URL).respond(json={"access_token": "new", "refresh_token": "r2", "expires_in": 3600})
    assert await client.refresh_access_token() is True
    assert await repo.get_state(auth_state.KEY_SOFT) is None


@pytest.mark.asyncio
async def test_note_does_not_weaken_an_existing_banner(env):
    repo, _, _ = env
    await auth_state.mark_needs_reconnect(repo)
    await auth_state.mark_renewal_failed(repo, None)
    assert await repo.get_state(auth_state.KEY_STATE) == auth_state.NEEDS_RECONNECT
    assert await repo.get_state(auth_state.KEY_SOFT) is None


class _FlakyDb:
    """Wraps the write-transaction connection and fails the n-th statement."""

    def __init__(self, db, fail_on):
        self._db, self._fail_on, self.calls = db, fail_on, 0

    async def execute(self, *args, **kwargs):
        self.calls += 1
        if self.calls == self._fail_on:
            raise RuntimeError("disk I/O error")
        return await self._db.execute(*args, **kwargs)


def _break_writes(repo, monkeypatch, attempts_to_break, fail_on=3):
    """Makes the first `attempts_to_break` write transactions fail on their `fail_on`-th statement."""
    real_write = repo.write
    seen = {"transactions": 0, "statements": []}

    @asynccontextmanager
    async def flaky_write():
        seen["transactions"] += 1
        async with real_write() as db:
            if seen["transactions"] <= attempts_to_break:
                db = _FlakyDb(db, fail_on)
            seen["statements"].append(db)
            yield db

    monkeypatch.setattr(repo, "write", flaky_write)
    return seen


@pytest.mark.asyncio
async def test_persist_tokens_is_one_transaction_and_rolls_back(env, monkeypatch):
    repo, _, _ = env
    await auth_state.persist_tokens(repo, "old-access", "old-refresh", 1000.0)
    seen = _break_writes(repo, monkeypatch, attempts_to_break=2)
    with pytest.raises(RuntimeError):
        await auth_state.persist_tokens(repo, "new-access", "new-refresh", 2000.0)
    assert seen["transactions"] == 2  # tried, retried once, gave up
    # the failed statement came after the access token's, but nothing of the new login was committed
    assert await repo.get_state("driver.simkl.access_token") == "old-access"
    assert await repo.get_state("driver.simkl.refresh_token") == "old-refresh"
    assert await auth_state.load_expiry(repo) == 1000.0


@pytest.mark.asyncio
async def test_persist_tokens_retries_once_then_succeeds(env, monkeypatch, caplog):
    repo, _, _ = env
    seen = _break_writes(repo, monkeypatch, attempts_to_break=1)
    with caplog.at_level(logging.WARNING, logger="penguwatch.drivers.simkl.auth"):
        await auth_state.persist_tokens(repo, "new-access", "new-refresh", 2000.0)
    assert seen["transactions"] == 2
    assert await repo.get_state("driver.simkl.access_token") == "new-access"
    assert await repo.get_state("driver.simkl.refresh_token") == "new-refresh"
    assert await auth_state.load_expiry(repo) == 2000.0
    assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL]


@pytest.mark.asyncio
async def test_persist_tokens_logs_critical_when_the_retry_fails_too(env, monkeypatch, caplog):
    repo, _, _ = env
    _break_writes(repo, monkeypatch, attempts_to_break=2)
    with caplog.at_level(logging.WARNING, logger="penguwatch.drivers.simkl.auth"):
        with pytest.raises(RuntimeError):
            await auth_state.persist_tokens(repo, "a", "r", 5.0)
    assert [r for r in caplog.records if r.levelno == logging.CRITICAL]


@pytest.mark.asyncio
async def test_persist_tokens_keeps_stored_refresh_token_when_none_given(env):
    repo, _, _ = env
    await auth_state.persist_tokens(repo, "a1", "r1", 100.0)
    await auth_state.persist_tokens(repo, "a2", None, None)
    assert await repo.get_state("driver.simkl.access_token") == "a2"
    assert await repo.get_state("driver.simkl.refresh_token") == "r1"
    assert await auth_state.load_expiry(repo) is None


def _outage_transport(token_status=503):
    """A SIMKL whose token endpoint answers `token_status` while every authenticated call rejects the old token."""
    calls = {"token": 0, "api": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            calls["token"] += 1
            if token_status == 200:
                return httpx.Response(200, json={"access_token": "fresh", "refresh_token": "r2", "expires_in": 3600})
            return httpx.Response(token_status)
        calls["api"] += 1
        if request.headers.get("authorization") != "Bearer fresh":
            return httpx.Response(401, json={"error": "user_token_failed"})
        return httpx.Response(200, json={"all": None, "tv_shows": {}, "anime": {}, "movies": {}})

    return httpx.MockTransport(handler), calls


@pytest.mark.asyncio
async def test_simkl_outage_during_renewal_is_not_a_reconnect_request(env):
    repo, client, sync = env
    transport, calls = _outage_transport()
    client._http = httpx.AsyncClient(transport=transport)
    client.set_tokens("old", "r1", time.time() - 10)  # expired: the pre-emptive refresh hits the outage

    result = await sync.sync(deep=False)

    assert result["status"] == "error"
    assert result["error"] == auth_state.UNREACHABLE_MSG
    assert client.last_refresh_outcome == "transient" and not client.refresh_rejected
    assert calls["token"] == 1  # the retry after the 401 was skipped by the 60 s backoff
    assert await repo.get_state(auth_state.KEY_STATE) is None  # no banner
    assert await repo.get_state(auth_state.KEY_MSG) is None
    assert await repo.get_state("driver.simkl.last_error") == auth_state.UNREACHABLE_MSG

    # SIMKL comes back: the next sync renews on its own and the message goes away
    transport2, calls2 = _outage_transport(token_status=200)
    client._http = httpx.AsyncClient(transport=transport2)
    client._refresh_backoff_until = 0.0
    assert (await sync.sync(deep=False))["status"] == "success"
    assert client.access_token == "fresh" and calls2["token"] == 1
    assert await repo.get_state("driver.simkl.last_error") is None
    assert await repo.get_state(auth_state.KEY_STATE) is None


@pytest.mark.asyncio
async def test_simkl_outage_during_renewal_does_not_raise_banner_from_push(env):
    repo, client, sync = env
    transport, _ = _outage_transport()
    client._http = httpx.AsyncClient(transport=transport)
    client.set_tokens("old", "r1", time.time() - 10)
    row = await repo.enqueue_outbox("simkl", SyncOp(kind="watched", media_type="show", ids={"simkl": 7}, native={"shows": [{"ids": {"simkl": 7}}]}), None, 7, approved=True)

    assert await sync.flush_outbox() == 0

    assert await repo.get_state(auth_state.KEY_STATE) is None
    assert await repo.get_state("driver.simkl.last_error") == auth_state.UNREACHABLE_MSG
    assert [o["id"] for o in await repo.get_outbox()] == [row]  # still queued for the next attempt


@pytest.mark.asyncio
async def test_login_without_refresh_token_still_asks_for_reconnect(env):
    repo, client, sync = env
    transport, _ = _outage_transport()
    client._http = httpx.AsyncClient(transport=transport)
    client.set_tokens("old", None, None)  # a pasted token cannot renew itself

    result = await sync.sync(deep=False)

    assert result["status"] == "error" and "Reconnect SIMKL" in result["error"]
    assert await repo.get_state(auth_state.KEY_STATE) == auth_state.NEEDS_RECONNECT


# --- a new login is authoritative (re-login without a refresh token) ---------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_pin_login_without_refresh_token_drops_the_old_one(container, monkeypatch):
    c = container
    repo = c.repository

    async def no_sync(*a, **k):
        return {}

    monkeypatch.setattr(c.sync, "sync", no_sync)
    await auth_state.persist_tokens(repo, "oldacc", "oldref", time.time() + 100)
    c.drivers.get("simkl").client.set_tokens("oldacc", "oldref", time.time() + 100)
    # the classic PIN fallback returns an access token and nothing else
    respx.get(f"{SIMKL_BASE_URL}/oauth/pin/ABCD").respond(json={"result": "OK", "access_token": "pinacc"})
    respx.post(f"{SIMKL_BASE_URL}/users/settings").respond(json={})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post("/api/drivers/simkl/connect", json={"state": {"user_code": "ABCD"}})
        assert res.json()["kind"] == "done"
        status = (await ac.get("/api/drivers/simkl/status")).json()
    assert c.drivers.get("simkl").client.access_token == "pinacc"
    assert c.drivers.get("simkl").client.refresh_token is None and c.drivers.get("simkl").client.token_expires_at is None
    assert await repo.get_state("driver.simkl.access_token") == "pinacc"
    assert await repo.get_state("driver.simkl.refresh_token") is None
    assert await auth_state.load_expiry(repo) is None
    assert status["can_auto_renew"] is False
    assert await c.drivers.get("simkl").client.renew_if_due() == "no_refresh_token"


@pytest.mark.asyncio
async def test_persist_tokens_new_login_replaces_the_stored_refresh_token(env):
    repo, _, _ = env
    await auth_state.persist_tokens(repo, "a1", "r1", 100.0)
    await auth_state.persist_tokens(repo, "a2", "r2", 200.0, new_login=True)
    assert await repo.get_state("driver.simkl.refresh_token") == "r2"
    await auth_state.persist_tokens(repo, "a3", None, None, new_login=True)
    assert await repo.get_state("driver.simkl.access_token") == "a3"
    assert await repo.get_state("driver.simkl.refresh_token") is None
    assert await auth_state.load_expiry(repo) is None


# --- a refresh in flight belongs to the login that started it; the client owns the exchange ---------------------


def gated_token_endpoint(response: httpx.Response):
    """A token endpoint that holds its answer until `release` is set; `started` is set once the POST arrived."""
    started, release, calls = asyncio.Event(), asyncio.Event(), []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        started.set()
        await release.wait()
        return response

    respx.post(TOKEN_URL).mock(side_effect=handler)
    return started, release, calls


@pytest.mark.asyncio
@respx.mock
async def test_refresh_answer_does_not_overwrite_a_newer_login(env):
    repo, client, _ = env
    saved = []
    inner = client.on_token_refreshed

    async def spy(a, r, e):
        saved.append(a)
        await inner(a, r, e)

    client.on_token_refreshed = spy
    client.set_tokens("old", "r1", None)
    started, release, _calls = gated_token_endpoint(
        httpx.Response(200, json={"access_token": "from-old-refresh", "refresh_token": "r2", "expires_in": 3600}))
    task = asyncio.create_task(client.refresh_access_token(stale_token="old"))
    await started.wait()
    client.set_tokens("fresh-login", "fresh-refresh", 5000.0)  # the user reconnects meanwhile
    release.set()
    assert await task is True  # the current login is fine; callers retry with it
    assert (client.access_token, client.refresh_token, client.token_expires_at) == ("fresh-login", "fresh-refresh", 5000.0)
    assert saved == [] and await repo.get_state("driver.simkl.access_token") is None


@pytest.mark.asyncio
@respx.mock
async def test_refused_refresh_does_not_condemn_a_newer_login(env):
    repo, client, _ = env
    rejected = []
    client.on_auth_rejected = lambda expires_at, still_current=None: _async_append(rejected, expires_at)
    client.set_tokens("old", "r1", None)
    started, release, _calls = gated_token_endpoint(httpx.Response(400, json={"error": "invalid_grant"}))
    task = asyncio.create_task(client.refresh_access_token(stale_token="old"))
    await started.wait()
    client.set_tokens("fresh-login", "fresh-refresh", None)
    release.set()
    assert await task is True
    assert not client.refresh_rejected and client.last_refresh_outcome != "rejected"
    assert rejected == [] and await repo.get_state(auth_state.KEY_SOFT) is None
    assert client.access_token == "fresh-login" and client.refresh_token == "fresh-refresh"


async def _async_append(items, value):
    items.append(value)


@pytest.mark.asyncio
@respx.mock
async def test_refresh_answer_does_not_resurrect_a_disconnected_login(container):
    c = container
    repo, client = c.repository, c.drivers.get("simkl").client
    wire(client, repo)
    await auth_state.persist_tokens(repo, "old", "r1", time.time() + 100)
    client.set_tokens("old", "r1", time.time() + 100)
    started, release, _calls = gated_token_endpoint(
        httpx.Response(200, json={"access_token": "zombie", "refresh_token": "r2", "expires_in": 3600}))
    task = asyncio.create_task(client.refresh_access_token(stale_token="old"))
    await started.wait()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.post("/api/drivers/simkl/disconnect")).json()["status"] == "disconnected"
    release.set()
    assert await task is False  # no login left
    assert client.access_token is None and client.refresh_token is None
    for key in ("driver.simkl.access_token", "driver.simkl.refresh_token", auth_state.KEY_EXPIRES):
        assert await repo.get_state(key) is None


@pytest.mark.asyncio
@respx.mock
async def test_refresh_answer_does_not_replace_a_pasted_token(container):
    c = container
    repo, client = c.repository, c.drivers.get("simkl").client
    wire(client, repo)
    client.set_tokens("old", "r1", None)
    started, release, _calls = gated_token_endpoint(
        httpx.Response(200, json={"access_token": "from-old-refresh", "refresh_token": "r2", "expires_in": 3600}))
    task = asyncio.create_task(client.refresh_access_token(stale_token="old"))
    await started.wait()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        await ac.post("/api/drivers/simkl/credentials", json={"client_id": "", "access_token": "pasted"})
    release.set()
    await task
    assert client.access_token == "pasted" and client.refresh_token is None
    assert await repo.get_state("driver.simkl.access_token") == "pasted"
    assert await repo.get_state("driver.simkl.refresh_token") is None


@pytest.mark.asyncio
@respx.mock
async def test_cancelled_caller_does_not_abort_the_exchange(env):
    repo, client, _ = env
    client.set_tokens("old", "r1", None)
    started, release, calls = gated_token_endpoint(
        httpx.Response(200, json={"access_token": "new", "refresh_token": "r2", "expires_in": 3600}))
    task = asyncio.create_task(client.refresh_access_token(stale_token="old"))
    await started.wait()
    task.cancel()  # e.g. the browser went away, or a request timed out, while SIMKL already had the refresh token
    with pytest.raises(asyncio.CancelledError):
        await task
    # a later caller joins the exchange in flight instead of spending the (single-use) refresh token twice
    joiner = asyncio.create_task(client.refresh_access_token(stale_token="old"))
    await asyncio.sleep(0)
    release.set()
    assert await joiner is True
    await client._refresh_task
    assert len(calls) == 1
    assert client.access_token == "new" and client.refresh_token == "r2"
    assert await repo.get_state("driver.simkl.access_token") == "new"
    assert await repo.get_state("driver.simkl.refresh_token") == "r2"


@pytest.mark.asyncio
@respx.mock
async def test_cancelled_caller_exchange_still_completes_without_other_callers(env):
    repo, client, _ = env
    client.set_tokens("old", "r1", None)
    started, release, _calls = gated_token_endpoint(
        httpx.Response(200, json={"access_token": "new", "refresh_token": "r2", "expires_in": 3600}))
    task = asyncio.create_task(client.refresh_access_token(stale_token="old"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await client._refresh_task
    assert client.access_token == "new"
    assert await repo.get_state("driver.simkl.refresh_token") == "r2"


# --- only a real OAuth refusal condemns the refresh token ---------------------------------------------------------

CLOUDFLARE_PAGE = "<html><head><title>Attention Required! | Cloudflare</title></head><body>Sorry, you have been blocked</body></html>"


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("response", [
    httpx.Response(403, text=CLOUDFLARE_PAGE, headers={"content-type": "text/html"}),
    httpx.Response(400, text=CLOUDFLARE_PAGE, headers={"content-type": "text/html"}),
    httpx.Response(400, json={"error": "rate_limit"}),
    httpx.Response(403, json={"error": "forbidden"}),
    httpx.Response(400, json={"message": "no error code at all"}),
    httpx.Response(400),
    httpx.Response(403, json=["not", "an", "object"]),
    httpx.Response(401, text="Unauthorized", headers={"content-type": "text/plain"}),
    httpx.Response(429, json={"error": "invalid_grant"}),
    httpx.Response(503, json={"error": "invalid_grant"}),
], ids=["cloudflare-403", "cloudflare-400", "rate-limit-400", "unknown-403", "no-error-400", "empty-400",
        "json-array-403", "plain-401", "429", "503"])
async def test_non_oauth_refusals_are_transient(env, response):
    repo, client, _ = env
    client.set_tokens("tok", "r1", None)
    token_route = respx.post(TOKEN_URL).mock(return_value=response)
    assert await client.refresh_access_token() is False
    assert not client.refresh_rejected and client.last_refresh_outcome == "transient"
    assert await repo.get_state(auth_state.KEY_SOFT) is None and await repo.get_state(auth_state.KEY_STATE) is None
    assert await client.refresh_access_token() is False
    assert token_route.call_count == 1  # backing off, not hammering
    client._refresh_backoff_until = 0.0
    token_route.mock(return_value=httpx.Response(200, json={"access_token": "new", "refresh_token": "r2"}))
    assert await client.refresh_access_token() is True  # the login was never condemned


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("status,body", [
    (400, {"error": "invalid_grant"}),
    (403, {"error": "invalid_grant", "error_description": "refresh token revoked"}),
    (400, {"error": "invalid_client"}),
    (401, {"error": "unauthorized_client"}),
    (401, {"error": "user_token_failed"}),  # a 401 with a JSON error body counts, whatever the code
], ids=["400-invalid_grant", "403-invalid_grant", "400-invalid_client", "401-unauthorized_client", "401-json"])
async def test_oauth_refusals_reject_the_refresh_token(env, status, body):
    repo, client, _ = env
    client.set_tokens("tok", "r1", None)
    respx.post(TOKEN_URL).respond(status_code=status, json=body)
    assert await client.refresh_access_token() is False
    assert client.refresh_rejected and client.last_refresh_outcome == "rejected"
    assert "Automatic renewal failed" in await repo.get_state(auth_state.KEY_SOFT)
    assert await repo.get_state(auth_state.KEY_STATE) is None  # still only a note until a call really fails


# --- a renewal whose save failed is saved on a later tick ---------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_failed_save_after_renewal_is_retried_on_the_next_tick(env, monkeypatch):
    repo, client, _ = env
    client.set_tokens("old", "r1", None)
    await auth_state.persist_tokens(repo, "old", "r1", None)
    respx.post(TOKEN_URL).respond(json={"access_token": "new", "refresh_token": "r2", "expires_in": 604800})
    seen = _break_writes(repo, monkeypatch, attempts_to_break=2)  # persist_tokens' own retry fails too
    assert await client.refresh_access_token() is True
    assert seen["transactions"] == 2
    assert client.access_token == "new" and client._pending_persist is not None
    assert await repo.get_state("driver.simkl.access_token") == "old"  # a restart now would lose the renewal

    assert await client.renew_if_due() == "not_needed"  # the next worker tick saves it first
    assert client._pending_persist is None
    assert await repo.get_state("driver.simkl.access_token") == "new"
    assert await repo.get_state("driver.simkl.refresh_token") == "r2"
    assert await auth_state.load_expiry(repo) is not None
    writes = seen["transactions"]
    await client.renew_if_due()
    assert seen["transactions"] == writes  # nothing left to save: no further writes


@pytest.mark.asyncio
@respx.mock
async def test_pending_save_keeps_retrying_and_keeps_only_the_latest_renewal(env):
    repo, client, _ = env
    client.set_tokens("old", "r1", None)
    attempts = []
    inner = client.on_token_refreshed
    broken = {"on": True}

    async def flaky(a, r, e):
        attempts.append(a)
        if broken["on"]:
            raise RuntimeError("disk full")
        await inner(a, r, e)

    client.on_token_refreshed = flaky
    token_route = respx.post(TOKEN_URL).respond(json={"access_token": "new1", "refresh_token": "r2", "expires_in": 3600})
    assert await client.refresh_access_token() is True
    assert client._pending_persist[1] == "new1"
    token_route.respond(json={"access_token": "new2", "refresh_token": "r3", "expires_in": 3600})
    # a second renewal while the first save is still owed: the failing save is retried first, then superseded
    assert await client.renew_if_due(margin=10 ** 9) == "renewed"
    assert client._pending_persist[1] == "new2" and "new1" in attempts
    assert await repo.get_state("driver.simkl.access_token") is None
    broken["on"] = False
    assert await client.renew_if_due(margin=0) == "not_needed"  # the tick that finally succeeds
    assert client._pending_persist is None
    assert await repo.get_state("driver.simkl.access_token") == "new2"
    assert await repo.get_state("driver.simkl.refresh_token") == "r3"


@pytest.mark.asyncio
@respx.mock
async def test_pending_save_is_dropped_when_the_login_changes(env):
    repo, client, _ = env
    client.set_tokens("old", "r1", None)
    broken = {"on": True}
    inner = client.on_token_refreshed

    async def flaky(a, r, e):
        if broken["on"]:
            raise RuntimeError("disk full")
        await inner(a, r, e)

    client.on_token_refreshed = flaky
    respx.post(TOKEN_URL).respond(json={"access_token": "new", "refresh_token": "r2", "expires_in": 3600})
    assert await client.refresh_access_token() is True
    assert client._pending_persist is not None
    client.set_tokens("fresh-login", None, None)  # the user reconnects before the save ever succeeded
    broken["on"] = False
    assert await client.flush_pending_persist() is True
    assert await repo.get_state("driver.simkl.access_token") is None  # the old renewal never overwrites the new login


# --- the banner clears as soon as the token is known to work ------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_banner_clears_even_when_a_later_sync_step_fails(env, monkeypatch):
    repo, client, sync = env
    await auth_state.mark_needs_reconnect(repo)
    mock_reads()

    async def boom(*a, **k):
        raise RuntimeError("enrichment exploded")

    monkeypatch.setattr(sync, "enrich_titles", boom)
    result = await sync.sync()
    assert result["status"] == "error" and "enrichment exploded" in result["error"]
    # get_activities succeeded, so the login demonstrably works: no stale banner
    assert await repo.get_state(auth_state.KEY_STATE) is None
    assert await repo.get_state(auth_state.KEY_MSG) is None


@pytest.mark.asyncio
@respx.mock
async def test_banner_clears_when_a_later_pull_request_fails(env):
    repo, _, sync = env
    await auth_state.mark_needs_reconnect(repo)
    mock_reads()
    respx.get(f"{SIMKL_BASE_URL}/sync/all-items/anime").respond(status_code=500, text="oops")
    assert (await sync.sync(deep=False))["status"] == "error"
    assert await repo.get_state(auth_state.KEY_STATE) is None


@pytest.mark.asyncio
@respx.mock
async def test_banner_clears_after_a_successful_push(env):
    repo, client, sync = env
    await auth_state.mark_needs_reconnect(repo)
    await repo.enqueue_outbox("simkl", SyncOp(kind="watched", media_type="show", ids={"simkl": 7}, native={"shows": [{"ids": {"simkl": 7}}]}), None, 7, approved=True)
    respx.post(f"{SIMKL_BASE_URL}/sync/history").respond(json={"added": {}})
    assert await sync.flush_outbox() == 1
    assert await repo.get_state(auth_state.KEY_STATE) is None and await repo.get_state(auth_state.KEY_MSG) is None


@pytest.mark.asyncio
@respx.mock
async def test_push_that_ends_in_a_401_keeps_the_banner(env):
    repo, client, sync = env
    client.set_tokens("tok", None, None)  # cannot renew itself
    await repo.enqueue_outbox("simkl", SyncOp(kind="watched", media_type="show", ids={"simkl": 7}, native={"shows": [{"ids": {"simkl": 7}}]}), None, 7, approved=True)
    await repo.enqueue_outbox("simkl", SyncOp(kind="unwatched", media_type="show", ids={"simkl": 8}, native={"shows": [{"ids": {"simkl": 8}}]}), None, 8, approved=True)
    respx.post(f"{SIMKL_BASE_URL}/sync/history").respond(json={"added": {}})
    respx.post(f"{SIMKL_BASE_URL}/sync/history/remove").respond(status_code=401, json={"error": "user_token_failed"})
    assert await sync.flush_outbox() == 1
    # the success settled nothing for good: the later 401 is what the user must see
    assert await repo.get_state(auth_state.KEY_STATE) == auth_state.NEEDS_RECONNECT


@pytest.mark.asyncio
@respx.mock
async def test_failed_activities_call_does_not_clear_the_banner(env):
    repo, client, sync = env
    client.set_tokens("tok", None, None)
    await auth_state.mark_needs_reconnect(repo)
    respx.get(ACTIVITIES_URL).respond(status_code=401, json={"error": "user_token_failed"})
    assert (await sync.sync(deep=False))["status"] == "error"
    assert await repo.get_state(auth_state.KEY_STATE) == auth_state.NEEDS_RECONNECT


# --- the worker leaves SIMKL alone while a reconnect is needed ----------------------------------------------------

HISTORY_URL = f"{SIMKL_BASE_URL}/sync/history"


async def _needs_reconnect_setup(repo, client):
    client.set_tokens("tok", "r1", None)
    client.refresh_rejected = True
    await auth_state.mark_needs_reconnect(repo)
    return await repo.enqueue_outbox("simkl", SyncOp(kind="watched", media_type="show", ids={"simkl": 7}, native={"shows": [{"ids": {"simkl": 7}}]}), None, 7, approved=True)


@pytest.mark.asyncio
@respx.mock
async def test_worker_pauses_push_and_pull_while_reconnect_is_needed(env):
    repo, client, sync = env
    row = await _needs_reconnect_setup(repo, client)
    act = respx.get(ACTIVITIES_URL).respond(status_code=401, json={"error": "user_token_failed"})
    hist = respx.post(HISTORY_URL).respond(status_code=401, json={"error": "user_token_failed"})
    token_route = respx.post(TOKEN_URL).respond(status_code=400, json={"error": "invalid_grant"})

    assert await worker_step(sync, pull_due=True) == "pulled"  # the one probe
    assert (act.call_count, hist.call_count) == (1, 1)
    assert await repo.get_state(auth_state.KEY_STATE) == auth_state.NEEDS_RECONNECT

    for due in (True, False, True, False):
        assert await worker_step(sync, pull_due=due) == "paused"
    assert (act.call_count, hist.call_count, token_route.call_count) == (1, 1, 0)
    outbox = await repo.get_outbox()
    assert [o["id"] for o in outbox] == [row] and outbox[0]["attempts"] == 0  # untouched

    sync._last_probe -= 3601  # an hour later: one more attempt, a full one even though only a push was due
    assert await worker_step(sync, pull_due=False) == "pulled"
    assert hist.call_count == 2 and act.call_count == 2
    assert await worker_step(sync, pull_due=True) == "paused"


@pytest.mark.asyncio
@respx.mock
async def test_hourly_probe_clears_the_banner_when_the_token_works_again(env):
    repo, client, sync = env
    await _needs_reconnect_setup(repo, client)
    sync._last_probe = None
    mock_reads()
    respx.post(HISTORY_URL).respond(json={"added": {}})
    assert await worker_step(sync, pull_due=True) == "pulled"
    assert sync.last_result["status"] == "success"
    assert await repo.get_state(auth_state.KEY_STATE) is None
    assert await sync.reconnect_gate() == "open"
    assert await repo.get_outbox() == []  # the queued change went out with the probe


@pytest.mark.asyncio
@respx.mock
async def test_worker_is_not_paused_when_renewal_may_still_work(env):
    repo, client, sync = env
    await auth_state.mark_needs_reconnect(repo)
    client.set_tokens("tok", "r1", None)  # e.g. after a restart: the refusal is only remembered in the database
    sync._last_probe = time.monotonic()
    assert await sync.reconnect_gate() == "open"
    client.refresh_rejected = False
    await auth_state.clear_needs_reconnect(repo)
    client.refresh_rejected = True  # refused, but no banner: the note is enough, sync carries on
    assert await sync.reconnect_gate() == "open"
    # a login that never had a refresh token cannot renew either, so it pauses too (after its probe)
    client.set_tokens("tok", None, None)
    await auth_state.mark_needs_reconnect(repo)
    assert await sync.reconnect_gate() == "paused"


@pytest.mark.asyncio
@respx.mock
async def test_a_failed_sync_counts_as_the_probe(env):
    repo, client, sync = env
    client.set_tokens("tok", None, None)
    respx.get(ACTIVITIES_URL).respond(status_code=401, json={"error": "user_token_failed"})
    assert (await sync.sync(deep=False))["status"] == "error"
    assert await repo.get_state(auth_state.KEY_STATE) == auth_state.NEEDS_RECONNECT
    assert await sync.reconnect_gate() == "paused"  # no second attempt straight after the one that just failed


def test_worker_loop_uses_worker_step():
    assert "worker_step" in inspect.getsource(sync_worker)


# --- review nits: probe reaches SIMKL, rejection note vs new login, save ordering -----------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_kick_woken_probe_with_empty_outbox_still_contacts_simkl(env):
    repo, client, sync = env
    client.set_tokens("tok", "r1", None)
    client.refresh_rejected = True
    await auth_state.mark_needs_reconnect(repo)
    assert await repo.get_outbox() == []
    sync._last_probe = None
    mock_reads()
    def act_calls():
        return sum(1 for c in respx.calls if str(c.request.url).startswith(ACTIVITIES_URL))

    # a worker tick woken by a kick: no pull due, nothing queued; the claimed probe must still reach SIMKL
    assert await worker_step(sync, pull_due=False) == "pulled"
    assert act_calls() == 1
    assert await repo.get_state(auth_state.KEY_STATE) is None  # the token works: banner gone
    assert await worker_step(sync, pull_due=False) == "flushed"


@pytest.mark.asyncio
@respx.mock
async def test_rejection_note_does_not_overwrite_a_login_that_landed_meanwhile(env):
    repo, client, _ = env
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def slow_rejected(expires_at, still_current=None):
        entered.set()
        await gate.wait()  # the widened window between the refusal and the note being written
        await auth_state.mark_renewal_failed(repo, expires_at, still_current)

    client.on_auth_rejected = slow_rejected
    client.set_tokens("old", "r1", None)
    respx.post(TOKEN_URL).respond(status_code=400, json={"error": "invalid_grant"})
    task = asyncio.create_task(client.refresh_access_token(stale_token="old"))
    await entered.wait()
    # the user logs in again while the callback is still pending
    client.set_tokens("fresh", "r9", None)
    await auth_state.persist_tokens(repo, "fresh", "r9", None, new_login=True)
    gate.set()
    await task
    assert await repo.get_state(auth_state.KEY_SOFT) is None  # the new login's clean slate survived
    assert not client.refresh_rejected


@pytest.mark.asyncio
async def test_mark_renewal_failed_still_writes_for_the_current_login(env):
    repo, _, _ = env
    await auth_state.mark_renewal_failed(repo, None, lambda: True)
    assert await repo.get_state(auth_state.KEY_SOFT)
    await repo.delete_state(auth_state.KEY_SOFT)
    await auth_state.mark_renewal_failed(repo, None, lambda: False)
    assert await repo.get_state(auth_state.KEY_SOFT) is None


@pytest.mark.asyncio
@respx.mock
async def test_slow_save_of_a_renewal_cannot_land_after_a_new_login(container, monkeypatch):
    c = container
    repo, client = c.repository, c.drivers.get("simkl").client

    async def no_sync(*a, **k):
        return {}

    monkeypatch.setattr(c.sync, "sync", no_sync)
    gate, saving = asyncio.Event(), asyncio.Event()

    async def slow_persist(access, refresh, expires_at):
        saving.set()
        await gate.wait()  # the save of the old login's renewal is slow (or being retried)
        await auth_state.persist_tokens(repo, access, refresh, expires_at)

    client.on_token_refreshed = slow_persist
    client.set_tokens("old", "r1", None)
    respx.post(TOKEN_URL).respond(json={"access_token": "renewed-old", "refresh_token": "r2", "expires_in": 3600})
    respx.get(f"{SIMKL_BASE_URL}/oauth/pin/ABCD").respond(json={"result": "OK", "access_token": "pinacc"})
    refresh = asyncio.create_task(client.refresh_access_token(stale_token="old"))
    await saving.wait()

    async def login():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            return await ac.post("/api/drivers/simkl/connect", json={"state": {"user_code": "ABCD"}})

    login_task = asyncio.create_task(login())
    for _ in range(5):
        await asyncio.sleep(0)
    gate.set()
    await refresh
    assert (await login_task).json()["kind"] == "done"
    assert client.access_token == "pinacc" and client.refresh_token is None
    assert await repo.get_state("driver.simkl.access_token") == "pinacc"  # the login's save came last
    assert await repo.get_state("driver.simkl.refresh_token") is None


@pytest.mark.asyncio
@respx.mock
async def test_slow_save_of_a_renewal_cannot_land_after_disconnect(container):
    c = container
    repo, client = c.repository, c.drivers.get("simkl").client
    gate, saving = asyncio.Event(), asyncio.Event()

    async def slow_persist(access, refresh, expires_at):
        saving.set()
        await gate.wait()
        await auth_state.persist_tokens(repo, access, refresh, expires_at)

    client.on_token_refreshed = slow_persist
    client.set_tokens("old", "r1", None)
    respx.post(TOKEN_URL).respond(json={"access_token": "renewed-old", "refresh_token": "r2", "expires_in": 3600})
    refresh = asyncio.create_task(client.refresh_access_token(stale_token="old"))
    await saving.wait()

    async def disconnect():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            return await ac.post("/api/drivers/simkl/disconnect")

    d = asyncio.create_task(disconnect())
    for _ in range(5):
        await asyncio.sleep(0)
    gate.set()
    await refresh
    assert (await d).json()["status"] == "disconnected"
    assert client.access_token is None
    for key in ("driver.simkl.access_token", "driver.simkl.refresh_token"):
        assert await repo.get_state(key) is None
