"""The local library driver (no account): what the engine, the worker and the API do with it, and the roles left empty."""

import pytest
import respx
from httpx import ASGITransport, AsyncClient

from penguwatch.api.deps import AppContainer, install
from penguwatch.config import AppConfig, DriversConfig
from penguwatch.db.repository import DatabaseRepository
from penguwatch.drivers.base import Capability, SyncError, SyncOp
from penguwatch.drivers.local import LOCAL_ONLY_MESSAGE, LocalDriver, LocalLibrary
from penguwatch.main import app, security_config
from penguwatch.models import MediaType, Show, ShowStatus
from penguwatch.optimizer.engine import SubscriptionOptimizer
from penguwatch.sync import worker
from tests.local_helpers import local_drivers, local_engine
from tests.simkl_helpers import simkl_drivers, simkl_engine
from penguwatch.drivers.simkl.client import SimklClient


@pytest.fixture
async def local_container(tmp_path):
    config = AppConfig(drivers=DriversConfig(library="local", schedule=[], discovery=[]))
    config.database.db_path = str(tmp_path / "local.db")
    repo = DatabaseRepository(config.database.db_path, key_providers=("simkl",))
    await repo.initialize([])
    engine = local_engine(repo)
    c = AppContainer(config=config, repository=repo, drivers=local_drivers(engine), sync=engine,
                     optimizer=SubscriptionOptimizer(repository=repo, config=config.optimizer),
                     security=security_config, notifier=None)
    install(app, c)
    yield c
    await repo.close()
    if hasattr(app.state, "container"):
        del app.state.container


async def get(path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.get(path)


async def test_the_local_driver_is_always_connected_and_says_it_is_local_only():
    library = LocalLibrary()
    status = await library.status()
    assert library.remote is False and library.is_connected() and not await library.needs_reconnect()
    assert (status.connected, status.auth_state, status.configured) == (True, "ok", True)
    assert status.message == LOCAL_ONLY_MESSAGE and "Local only" in status.message
    step = await library.connect()
    assert step.kind == "error"  # there is no connect step
    assert LocalDriver.offers == frozenset({Capability.LIBRARY_SYNC}) and not hasattr(LocalDriver, "connect_kind")
    with pytest.raises(SyncError):
        await library.push([SyncOp(kind="status", media_type="show", ids={})])


@respx.mock
async def test_the_worker_does_no_pulls_and_nothing_touches_the_network(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "w.db"))
    await repo.initialize([])
    try:
        engine = local_engine(repo)
        calls = []
        engine.library.plan_pull = lambda *a, **k: calls.append("plan_pull")
        engine.library.pull = lambda *a, **k: calls.append("pull")
        assert await worker.worker_step(engine, pull_due=True) == "pulled"
        assert await worker.worker_step(engine, pull_due=False) == "flushed"
        assert calls == [] and not respx.calls
        assert engine.library.pull_interval_minutes > 0
    finally:
        await repo.close()


async def test_a_run_with_the_local_library_still_does_the_catalogue_upkeep(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "u.db"))
    await repo.initialize([])
    try:
        engine = local_engine(repo)
        asked = []
        engine.refresh_episode_lists = lambda: asked.append("episodes") or _zero()
        result = await engine.sync()
        assert result["status"] == "success" and result["pushed"] == 0 and result["mode"] == "none"
        assert asked == ["episodes"]
    finally:
        await repo.close()


async def _zero():
    return 0


async def test_api_drivers_reports_the_local_library_and_what_waits_for_an_inactive_driver(local_container):
    c = local_container
    show = await c.repository.upsert_show(Show(title="Alpha", ids={"simkl": 1}, media_type=MediaType.SHOW))
    simkl = simkl_engine(SimklClient(client_id="x", access_token="y"), c.repository)
    try:
        await simkl.update_show_status(show, ShowStatus.HOLD)
    finally:
        await simkl.service.aclose()
    data = (await get("/api/drivers")).json()
    assert data["library"] == "local" and data["remote"] is False and data["pull_interval_minutes"] > 0
    assert data["schedule"] == [] and data["discovery"] == [] and data["catalog"] == ["simkl"]
    assert data["inactive_outbox"] == {"simkl": 1} and data["waiting_for_inactive"] == 1
    by_name = {d["name"]: d for d in data["drivers"]}
    assert by_name["local"]["remote"] is False and by_name["local"]["connect_kind"] is None
    assert by_name["local"]["extras"] == [] and by_name["local"]["title_urls"] == {}
    simkl_info = by_name["simkl"]
    assert simkl_info["remote"] is True and simkl_info["connect_kind"] == "device_code"
    assert simkl_info["extras"] == ["credentials", "diagnose"]
    assert simkl_info["title_urls"]["movie"] == "https://simkl.com/movies/{id}"
    assert simkl_info["takes_links"] is True and by_name["local"]["takes_links"] is False  # Add title's hint names SIMKL
    assert [r["id"] for r in simkl_info["rating_sources"]] == ["simkl", "imdb"]

    status = (await get("/api/drivers/local/status")).json()
    assert (status["authenticated"], status["auth_state"], status["remote"]) == (True, "ok", False)
    assert status["review_mode"] is False and status["auth_message"] == LOCAL_ONLY_MESSAGE
    assert status["waiting_for_inactive"] == 1 and status["pending_pushes"] == 0 and status["awaiting_approval"] == 0
    assert (await get("/api/drivers/simkl/status")).status_code == 404  # simkl is not running here


async def test_api_drivers_for_the_default_configuration_names_simkl_as_an_account(container):
    data = (await get("/api/drivers")).json()
    assert data["library"] == "simkl" and data["remote"] is True and data["inactive_outbox"] == {}
    assert (await get("/api/drivers/simkl/status")).json()["remote"] is True


async def test_with_no_schedule_driver_the_upcoming_list_and_the_feed_hold_no_episodes(local_container):
    c = local_container
    show = await c.repository.upsert_show(Show(title="Alpha", ids={"simkl": 1}, media_type=MediaType.SHOW))
    await c.repository.update_show_fields(show, {"next_episode_air_date": "2099-01-01", "next_episode_season": 1,
                                                 "next_episode_number": 2})
    assert (await get("/api/calendar/upcoming?days_ahead=100000")).json() == []
    feed = (await get("/api/calendar.ics?days_ahead=100000")).text
    assert "BEGIN:VCALENDAR" in feed and "S01E02" not in feed
    c.drivers.schedules = [object()]
    assert len((await get("/api/calendar/upcoming?days_ahead=100000")).json()) == 1
    assert "S01E02" in (await get("/api/calendar.ics?days_ahead=100000")).text


async def test_with_no_discovery_driver_discover_says_so(local_container):
    assert (await get("/api/discover/recommendations")).json() == {"enabled": False, "items": []}
    assert (await get("/api/discover/trending")).status_code == 400
