"""Polite SIMKL usage: no repeated delta fetches while changes await review, paced per-title calls,
idle backoff for the background timer."""

import json

import pytest
import respx

from penguwatch import clock
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import ServiceConfig
from penguwatch.drivers.simkl.client import SimklClient, SIMKL_BASE_URL
from tests.simkl_helpers import simkl_engine

from tests.test_two_way_sync import ACTIVITIES_1, SEVERANCE, mock_reads


@pytest.fixture
async def env(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "polite.db"))
    await repo.initialize([ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99, aliases=["Apple TV+"])])
    client = SimklClient(client_id="cid", access_token="tok")
    client.retry_base_delay = 0
    sync = simkl_engine(client, repo)
    yield repo, client, sync
    await client.aclose()
    await repo.close()


def moved_activities(stamp="2026-10-05T10:00:00Z"):
    a = json.loads(json.dumps(ACTIVITIES_1))
    a["all"] = a["tv_shows"]["all"] = a["tv_shows"]["watching"] = stamp
    return a


def urls(since=0):
    return [str(c.request.url.path) for c in list(respx.calls)[since:]]


async def stage_one_change(sync, remote_status="hold"):
    """Import the library, then let SIMKL move Severance so one change is staged for review."""
    mock_reads()
    await sync.sync(deep=False)  # first import into an empty library applies directly
    remote = dict(SEVERANCE, status=remote_status)
    respx.get(f"{SIMKL_BASE_URL}/sync/activities").respond(json=moved_activities())
    respx.get(f"{SIMKL_BASE_URL}/sync/all-items/shows").respond(json={"shows": [remote]})
    return await sync.sync(deep=False)


# ------------------------------------------------------------ 1. no re-fetch of an unreviewed delta

@pytest.mark.asyncio
@respx.mock
async def test_unchanged_activities_while_staged_costs_one_request(env):
    repo, client, sync = env
    first = await stage_one_change(sync)
    assert first["staged"] == 1
    staged_before = await repo.get_state("incoming_staged")
    snapshot_before = await repo.get_state("driver.simkl.cursor")

    mark = len(respx.calls)
    second = await sync.sync(deep=False)

    assert urls(mark) == ["/sync/activities"]
    assert second["status"] == "success"
    assert second["staged"] == 1 and second["updated"] == 0
    assert second["skipped"] == "unchanged since staged"
    assert await repo.get_state("incoming_staged") == staged_before
    assert await repo.get_state("driver.simkl.cursor") == snapshot_before


@pytest.mark.asyncio
@respx.mock
async def test_changed_activities_refetch_the_delta_and_stage_new_items(env):
    repo, client, sync = env
    await stage_one_change(sync)

    # SIMKL moves again: a new show appears in addition to the staged change
    new_show = {
        "status": "plantowatch", "user_rating": None, "watched_episodes_count": 0, "total_episodes_count": 8,
        "show": {"title": "Slow Horses", "year": 2022, "ids": {"simkl": 777001}},
    }
    respx.get(f"{SIMKL_BASE_URL}/sync/activities").respond(json=moved_activities("2026-10-06T10:00:00Z"))
    respx.get(f"{SIMKL_BASE_URL}/sync/all-items/shows").respond(
        json={"shows": [dict(SEVERANCE, status="hold"), new_show]})
    mark = len(respx.calls)
    result = await sync.sync(deep=False)

    assert "/sync/all-items/shows" in urls(mark)
    assert "skipped" not in result
    assert result["staged"] == 2
    incoming = await sync.get_incoming()
    assert {e["kind"] for e in incoming["entries"]} == {"update", "new"}


@pytest.mark.asyncio
@respx.mock
async def test_without_staged_record_unchanged_all_is_still_none(env):
    repo, client, sync = env
    mock_reads()
    await sync.sync(deep=False)
    assert await repo.get_state("incoming_staged") is None

    mark = len(respx.calls)
    result = await sync.sync(deep=False)
    assert urls(mark) == ["/sync/activities"]
    assert result["updated"] == 0 and "skipped" not in result
    current = await client.get_activities()
    saved = json.loads(await repo.get_state("driver.simkl.cursor"))
    assert sync.library._pull_plan(False, current, saved) == ("none", [])


@pytest.mark.asyncio
@respx.mock
async def test_force_full_ignores_the_staged_shortcut(env):
    repo, client, sync = env
    await stage_one_change(sync)
    mark = len(respx.calls)
    await sync.sync(force_full=True, deep=False)
    assert "/sync/all-items/shows" in urls(mark)


# ------------------------------------------------------------ 2. paced, capped per-title calls

from penguwatch.models import MediaType, Show, ShowStatus  # noqa: E402
from penguwatch.sync import enrich  # noqa: E402


@pytest.fixture
def no_spacing(monkeypatch):
    monkeypatch.setattr(enrich, "CALL_SPACING_S", 0)


@pytest.fixture
def timeline(monkeypatch):
    """Records start/end of each per-title call and every pause between them (without really sleeping)."""
    events = []

    async def fake_sleep(seconds):
        events.append(("sleep", seconds))

    monkeypatch.setattr(enrich.asyncio, "sleep", fake_sleep)
    return events


async def add_shows(repo, n, first_id=9000, **kwargs):
    return [await repo.upsert_show(Show(title=f"Show {i}", ids={"simkl": first_id + i}, **kwargs)) for i in range(n)]


def stub_episode_refresh(sync, events=None):
    done = []

    async def refresh(show):
        done.append(show.id)
        if events is not None:
            events.append(("start", show.id))
        await sync.repository.update_show_fields(show.id, {"episodes_synced_at": clock.utc_now().isoformat()})
        if events is not None:
            events.append(("end", show.id))
        return True

    sync._refresh_show_episodes = refresh
    return done


@pytest.mark.asyncio
async def test_episode_lists_run_one_at_a_time_with_spacing_between(env, timeline):
    repo, client, sync = env
    ids = await add_shows(repo, 3, status=ShowStatus.WATCHING, media_type=MediaType.SHOW)
    stub_episode_refresh(sync, timeline)

    assert await sync.refresh_episode_lists(limit=10) == 3

    kinds = [e[0] for e in timeline]
    assert kinds == ["start", "end", "sleep", "start", "end", "sleep", "start", "end"]  # none after the last
    assert [e[1] for e in timeline if e[0] == "sleep"] == [enrich.CALL_SPACING_S] * 2
    assert enrich.CALL_SPACING_S == 0.5
    assert sorted(e[1] for e in timeline if e[0] == "start") == sorted(ids)


@pytest.mark.asyncio
async def test_episode_list_cap_and_remainder_next_call(env, no_spacing):
    repo, client, sync = env
    await add_shows(repo, 30, status=ShowStatus.WATCHING, media_type=MediaType.SHOW)
    done = stub_episode_refresh(sync)

    assert await sync.refresh_episode_lists() == 25  # default cap
    assert len(done) == 25 and len(set(done)) == 25
    assert await sync.refresh_episode_lists() == 5  # the rest, nothing refreshed twice
    assert len(set(done)) == 30
    assert await sync.refresh_episode_lists() == 0


@pytest.mark.asyncio
async def test_episode_lists_keep_watching_first_when_capped(env, no_spacing):
    repo, client, sync = env
    done_ids = await add_shows(repo, 3, status=ShowStatus.COMPLETED, media_type=MediaType.SHOW)
    watching = await add_shows(repo, 2, first_id=8000, status=ShowStatus.WATCHING, media_type=MediaType.SHOW)
    done = stub_episode_refresh(sync)

    assert await sync.refresh_episode_lists(limit=2) == 2
    assert sorted(done) == sorted(watching)
    assert not set(done) & set(done_ids)


@pytest.mark.asyncio
async def test_sequel_checks_are_paced_capped_and_resumed(env, timeline):
    repo, client, sync = env
    await add_shows(repo, 30, status=ShowStatus.COMPLETED, media_type=MediaType.ANIME)
    requested = []

    async def get_details(media, simkl_id):
        requested.append(simkl_id)
        return {}

    client.get_details = get_details

    assert await sync.refresh_sequels() == 25
    assert len(requested) == 25
    assert [e[0] for e in timeline].count("sleep") == 24
    assert await sync.refresh_sequels() == 5
    assert len(set(requested)) == 30


@pytest.mark.asyncio
async def test_enrich_titles_sequential_paced_and_capped(env, timeline):
    repo, client, sync = env
    await add_shows(repo, 30, status=ShowStatus.WATCHING, media_type=MediaType.SHOW)
    requested = []

    async def get_details(media, simkl_id):
        timeline.append(("start", simkl_id))
        requested.append(simkl_id)
        timeline.append(("end", simkl_id))
        return {"overview": "x"}

    client.get_details = get_details

    assert await sync.enrich_titles() == 25
    assert len(requested) == 25
    pattern = [e[0] for e in timeline]
    assert pattern[:6] == ["start", "end", "sleep", "start", "end", "sleep"]
    assert pattern.count("sleep") == 24
    assert await sync.enrich_titles() == 5


@pytest.mark.asyncio
async def test_calls_for_a_waiting_person_are_not_delayed(env, timeline):
    repo, client, sync = env
    ids = await add_shows(repo, 3, status=ShowStatus.WATCHING, media_type=MediaType.SHOW)

    async def get_details(media, simkl_id):
        return {"overview": "x"}

    client.get_details = get_details
    assert await sync.enrich_titles(ids, pace=False) == 3

    stub_episode_refresh(sync)
    show = await repo.get_show_by_id(ids[0])
    assert await sync.ensure_episode_list(show) is True
    assert [e for e in timeline if e[0] == "sleep"] == []


# ------------------------------------------------------------ 3. idle backoff for the background timer

from penguwatch.activity import (  # noqa: E402
    ActivityMiddleware, ActivityTracker, is_user_interaction, pick_interval_minutes,
)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.mark.parametrize("method,path,counts", [
    ("GET", "/", True),
    ("GET", "/api/shows", True),
    ("GET", "/api/shows/42?x=1", True),
    ("GET", "/api/up-next/", False),  # polled by the open page
    ("GET", "/api/drivers/simkl/status", False),  # polled by the open page
    ("GET", "/api/health", False),
    ("GET", "/api/health/ready", False),
    ("GET", "/api/system", False),
    ("GET", "/metrics", False),
    ("GET", "/static/app.js", False),
    ("GET", "/login", False),
    ("POST", "/api/shows/42/status", True),
    ("PUT", "/api/up-next", True),  # mutating requests always count
    ("DELETE", "/api/shows/42", True),
    ("PATCH", "/api/shows/42", True),
    ("POST", "/api/drivers/simkl/status", True),
    ("GET", "/api/up-next-extra", True),  # only the exact polled path is excluded
])
def test_which_requests_are_user_interaction(method, path, counts):
    assert is_user_interaction(method, path) is counts


def test_fresh_tracker_is_active_and_goes_idle_after_threshold():
    clock = FakeClock()
    t = ActivityTracker(idle_after_seconds=3600, clock=clock)
    assert not t.is_idle() and t.age_seconds() == 0
    clock.now += 3600
    assert not t.is_idle()  # exactly at the threshold is still in use
    clock.now += 1
    assert t.is_idle() and t.age_seconds() == 3601


def test_polling_does_not_keep_the_timer_awake_but_use_does():
    clock = FakeClock()
    t = ActivityTracker(idle_after_seconds=3600, clock=clock)
    clock.now += 3000
    assert t.record("GET", "/api/drivers/simkl/status") is False
    clock.now += 3000
    assert t.is_idle()  # the poll at +3000 s did not reset the clock
    assert t.record("GET", "/api/shows") is True
    assert not t.is_idle() and t.age_seconds() == 0


def test_resume_is_reported_once_and_notifies():
    clock = FakeClock()
    woke = []
    t = ActivityTracker(idle_after_seconds=60, clock=clock, on_resume=lambda: woke.append(1))
    assert t.touch() is False and not t.take_resumed()
    clock.now += 61
    assert t.touch() is True
    assert woke == [1]
    assert t.take_resumed() is True
    assert t.take_resumed() is False
    t.touch()  # already active again: not a resume
    assert woke == [1] and not t.take_resumed()


def test_interval_selection():
    assert pick_interval_minutes(False, 30, 180) == 30
    assert pick_interval_minutes(True, 30, 180) == 180
    assert pick_interval_minutes(True, 30, 10) == 30  # idle never polls more often than active


@pytest.mark.asyncio
async def test_middleware_records_requests_that_reach_the_app():
    clock = FakeClock()
    t = ActivityTracker(idle_after_seconds=60, clock=clock)
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["path"])

    mw = ActivityMiddleware(inner, t)
    clock.now += 100
    await mw({"type": "http", "method": "GET", "path": "/api/system"}, None, None)
    assert t.is_idle()
    await mw({"type": "websocket", "path": "/api/shows"}, None, None)
    assert t.is_idle()
    await mw({"type": "http", "method": "GET", "path": "/api/shows"}, None, None)
    assert not t.is_idle() and t.take_resumed()
    assert seen == ["/api/system", "/api/shows", "/api/shows"]


def test_defaults_in_config():
    from penguwatch.drivers.simkl.options import SimklOptions as SimklConfig
    cfg = SimklConfig()
    assert (cfg.auto_sync_interval_minutes, cfg.idle_after_hours, cfg.idle_sync_interval_minutes) == (30, 12, 180)
