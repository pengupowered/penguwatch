"""Daily housekeeping, start-up PRAGMA optimize, and the worker's wait while no account is connected."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import aiosqlite
import pytest

from penguwatch import clock
from penguwatch.db.repository import DatabaseRepository
from penguwatch.sync import worker


@pytest.fixture
async def repo(tmp_path):
    r = DatabaseRepository(str(tmp_path / "house.db"))
    await r.initialize([])
    yield r
    await r.close()


@pytest.mark.asyncio
async def test_housekeeping_drops_notified_markers_older_than_60_days(repo):
    now = clock.utc_now()
    await repo.set_state("notified:old", (now - timedelta(days=61)).isoformat())
    await repo.set_state("notified:edge", (now - timedelta(days=59)).isoformat())
    await repo.set_state("notified:new", now.isoformat())
    await repo.set_state("other:old", (now - timedelta(days=300)).isoformat())  # not a marker
    await repo.set_state("sync_review_mode", "off")
    assert await worker.housekeeping(repo) == 1
    left = {k for k in ("notified:old", "notified:edge", "notified:new", "other:old", "sync_review_mode")
            if await repo.get_state(k) is not None}
    assert left == {"notified:edge", "notified:new", "other:old", "sync_review_mode"}


@pytest.mark.asyncio
async def test_pragma_optimize_runs_at_start_up_after_the_migrations(tmp_path, monkeypatch):
    seen = []
    original = aiosqlite.Connection.execute

    def spy(self, sql, *a, **k):
        seen.append(str(sql).strip())
        return original(self, sql, *a, **k)
    monkeypatch.setattr(aiosqlite.Connection, "execute", spy)
    r = DatabaseRepository(str(tmp_path / "opt.db"))
    await r.initialize([])
    await r.close()
    assert "PRAGMA optimize" in seen
    assert seen.index("PRAGMA optimize") > max(i for i, q in enumerate(seen) if "schema_migrations" in q)


class _Library:
    remote = True
    label = "Acct"
    idle_after_hours = 12

    def __init__(self, connected):
        self.connected = connected

    def is_connected(self):
        return self.connected

    async def maintain(self):
        return None


def _engine(repo, connected):
    async def nothing(*a, **k):
        return {}

    async def gate():
        return "open"
    return SimpleNamespace(library=_Library(connected), repository=repo, kick=asyncio.Event(), reconnect_gate=gate,
                           sync=nothing, flush_outbox=nothing)


class _Shim:
    """asyncio as the worker module sees it: no 5 s start-up pause, and waits recorded and shortened."""
    TimeoutError = asyncio.TimeoutError
    CancelledError = asyncio.CancelledError
    get_running_loop = staticmethod(asyncio.get_running_loop)

    def __init__(self, shorten=True):
        self.waits = []
        self.shorten = shorten

    async def sleep(self, seconds):
        await asyncio.sleep(0)

    async def wait_for(self, awaitable, timeout):
        self.waits.append(timeout)
        return await asyncio.wait_for(awaitable, timeout=0.05 if self.shorten else timeout)


async def _run_worker(engine, monkeypatch, until, shorten=True, kick_after=None):
    shim = _Shim(shorten)
    monkeypatch.setattr(worker, "asyncio", shim)
    task = asyncio.ensure_future(worker.sync_worker(engine, 15, 60, SimpleNamespace(
        take_resumed=lambda: False, is_idle=lambda: False)))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if kick_after is not None and len(shim.waits) >= kick_after:
            engine.kick.set()
            kick_after = None
        if until(shim):
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return shim


@pytest.mark.asyncio
async def test_the_worker_waits_a_minute_while_nothing_is_connected(repo, monkeypatch):
    engine = _engine(repo, connected=False)
    shim = await _run_worker(engine, monkeypatch, until=lambda s: len(s.waits) >= 1)
    assert shim.waits[0] == worker.NOT_CONNECTED_WAIT_S == 60.0


@pytest.mark.asyncio
async def test_a_kick_ends_the_minute_long_wait_at_once(repo, monkeypatch):
    """What a connect does (api/drivers.py): the worker is not left to wait out its 60 s."""
    engine = _engine(repo, connected=False)
    shim = await _run_worker(engine, monkeypatch, until=lambda s: len(s.waits) >= 2, shorten=False, kick_after=1)
    assert shim.waits[:2] == [60.0, 60.0]  # the second wait only began because the first ended: nobody waited 60 s


@pytest.mark.asyncio
async def test_the_worker_keeps_its_short_waits_while_connected(repo, monkeypatch):
    shim = await _run_worker(_engine(repo, connected=True), monkeypatch, until=lambda s: len(s.waits) >= 1)
    assert 5.0 <= shim.waits[0] <= 120.0


@pytest.mark.asyncio
async def test_the_worker_runs_housekeeping_once_a_day(repo, monkeypatch):
    calls = []

    async def fake(r):
        calls.append(1)
        return 0
    monkeypatch.setattr(worker, "housekeeping", fake)
    await _run_worker(_engine(repo, connected=False), monkeypatch, until=lambda s: len(s.waits) >= 3)
    assert calls == [1]  # at start, and not again for 24 h


@pytest.mark.asyncio
async def test_connecting_and_pasting_a_token_kick_the_worker(repo):
    from penguwatch.api.drivers import driver_connect, driver_credentials
    from penguwatch.api.schemas import ConnectRequest, DriverCredentialsRequest
    from penguwatch.drivers.base import ConnectStep

    class Library(_Library):
        def __init__(self):
            super().__init__(False)

        async def connect(self, state=None):
            return ConnectStep(kind="done")

        async def set_credentials(self, values):
            return {"token_set": True}

    lib = Library()
    sync = SimpleNamespace(library=lib, kick=asyncio.Event(), sync=lambda **k: asyncio.sleep(0))
    drivers = SimpleNamespace(get=lambda name: SimpleNamespace(library=lib))
    container = SimpleNamespace(background=set())
    await driver_connect("acct", ConnectRequest(), drivers=drivers, sync=sync, c=container)
    assert sync.kick.is_set()
    sync.kick.clear()
    await driver_credentials("acct", DriverCredentialsRequest(access_token="x"), drivers=drivers, sync=sync)
    assert sync.kick.is_set()
    for task in list(container.background):
        await task
