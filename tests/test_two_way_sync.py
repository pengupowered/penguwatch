"""Two-way SIMKL sync: delta pulls, exact episode pushes, outbox retries, conflict protection."""

import json
from datetime import date, timedelta

import pytest
import respx
import httpx

from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import ServiceConfig, ShowStatus, Priority, HouseholdTag, MediaType
from penguwatch.drivers.simkl.client import SimklClient, SIMKL_BASE_URL
from tests.simkl_helpers import simkl_engine
from penguwatch.drivers.simkl.payloads import build as simkl_body

PAST = (date.today() - timedelta(days=30)).isoformat()
FUTURE = (date.today() + timedelta(days=10)).isoformat()

ACTIVITIES_1 = {
    "all": "2026-10-01T10:00:00Z",
    "tv_shows": {"all": "2026-10-01T10:00:00Z", "watching": "2026-10-01T10:00:00Z", "rated_at": None, "removed_from_list": None},
    "anime": {"all": None},
    "movies": {"all": "2026-09-01T10:00:00Z", "completed": "2026-09-01T10:00:00Z"},
}

SEVERANCE = {
    "status": "watching",
    "user_rating": 9,
    "watched_episodes_count": 2,
    "total_episodes_count": 4,
    "not_aired_episodes_count": 1,
    "last_watched_at": "2026-09-30T20:00:00Z",
    "next_to_watch_info": {"title": "Ep 3", "season": 1, "episode": 3, "date": PAST},
    "show": {"title": "Severance", "year": 2022, "network": "Apple TV+", "ids": {"simkl": 1400298, "tmdb": "95396"}},
    "seasons": [{"number": 1, "episodes": [{"number": 1, "watched_at": "2026-09-29T20:00:00Z"},
                                          {"number": 2, "watched_at": "2026-09-30T20:00:00Z"}]}],
}

MOVIE = {
    "status": "completed",
    "user_rating": 8,
    "last_watched_at": "2026-09-01T20:00:00Z",
    "movie": {"title": "Dune: Part Two", "year": 2024, "runtime": 166, "ids": {"simkl": 2222, "tmdb": "693134"}},
}

EPISODES = [
    {"title": "Good News About Hell", "season": 1, "episode": 1, "type": "episode", "date": PAST},
    {"title": "Half Loop", "season": 1, "episode": 2, "type": "episode", "date": PAST},
    {"title": "In Perpetuity", "season": 1, "episode": 3, "type": "episode", "date": PAST},
    {"title": "The You You Are", "season": 2, "episode": 1, "type": "episode", "date": FUTURE},
    {"title": "Special", "season": 0, "episode": 1, "type": "special", "date": PAST},
]


@pytest.fixture
async def env(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "sync.db"))
    await repo.initialize([ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99, aliases=["Apple TV+"])])
    client = SimklClient(client_id="cid", access_token="tok")
    client.retry_base_delay = 0
    sync = simkl_engine(client, repo)
    await repo.set_state("sync_review_mode", "off")  # these tests cover auto-sync; review mode is tested separately
    yield repo, client, sync
    await client.aclose()
    await repo.close()


def mock_reads(activities=ACTIVITIES_1, shows=None, movies=None):
    respx.get(f"{SIMKL_BASE_URL}/sync/activities").respond(json=activities)
    respx.get(f"{SIMKL_BASE_URL}/sync/all-items/shows").respond(json={"shows": shows if shows is not None else [SEVERANCE]})
    respx.get(f"{SIMKL_BASE_URL}/sync/all-items/anime").respond(json={})
    respx.get(f"{SIMKL_BASE_URL}/sync/all-items/movies").respond(json={"movies": movies if movies is not None else [MOVIE]})
    respx.get(url__regex=rf"{SIMKL_BASE_URL}/(tv|movies|anime)/\d+$").respond(json={})
    respx.get(f"{SIMKL_BASE_URL}/tv/episodes/1400298").respond(json=EPISODES)


@pytest.mark.asyncio
@respx.mock
async def test_initial_full_pull_imports_shows_movies_and_episode_state(env):
    repo, client, sync = env
    mock_reads()
    result = await sync.sync(deep=False)
    assert result["status"] == "success"
    assert result["mode"] == "full"

    sev = await repo.get_show_by_external_id("simkl", 1400298)
    assert sev.status == ShowStatus.WATCHING
    assert sev.user_rating == 9
    assert sev.next_to_watch == "S01E03"
    assert sev.service_id == "apple_tv"
    eps = await repo.get_episodes(sev.id)
    assert {(e.season, e.number) for e in eps if e.is_watched} == {(1, 1), (1, 2)}

    movie = await repo.get_show_by_external_id("simkl", 2222)
    assert movie.media_type == MediaType.MOVIE
    assert movie.status == ShowStatus.COMPLETED
    assert movie.watched_episodes == 1 and movie.episodes_left == 0
    # Snapshot stored only after success
    assert json.loads(await repo.get_state("driver.simkl.cursor"))["all"] == ACTIVITIES_1["all"]


@pytest.mark.asyncio
@respx.mock
async def test_unchanged_activities_means_no_library_fetch(env):
    repo, client, sync = env
    mock_reads()
    await sync.sync(deep=False)
    route = respx.get(f"{SIMKL_BASE_URL}/sync/all-items/shows")
    before = route.call_count
    result = await sync.sync(deep=False)
    assert result["status"] == "success"
    assert route.call_count == before


@pytest.mark.asyncio
@respx.mock
async def test_local_priority_and_household_survive_sync(env):
    repo, client, sync = env
    mock_reads()
    await sync.sync(deep=False)
    sev = await repo.get_show_by_external_id("simkl", 1400298)
    await repo.update_show_metadata(sev.id, priority=Priority.HIGH, household_tag=HouseholdTag.BOTH)
    await sync.sync(force_full=True, deep=False)
    sev = await repo.get_show_by_external_id("simkl", 1400298)
    assert sev.priority == Priority.HIGH
    assert sev.household_tag == HouseholdTag.BOTH


@pytest.mark.asyncio
@respx.mock
async def test_plus_one_pushes_exact_season_and_episode(env):
    repo, client, sync = env
    mock_reads()
    await sync.sync(deep=False)
    sev = await repo.get_show_by_external_id("simkl", 1400298)

    updated = await sync.mark_next_episode(sev.id)
    assert updated.watched_episodes == 3
    assert updated.aired_episodes == 3  # S02E01 hasn't aired, special ignored
    assert updated.next_to_watch is None
    assert updated.next_episode_air_date == FUTURE

    outbox = await repo.get_due_outbox("simkl")
    assert len(outbox) == 1 and outbox[0]["op"] == "watched"
    season = simkl_body(outbox[0]["operation"])["shows"][0]["seasons"][0]
    assert season["number"] == 1 and season["episodes"][0]["number"] == 3

    hist = respx.post(f"{SIMKL_BASE_URL}/sync/history").respond(json={"added": {"episodes": 1}})
    assert await sync.flush_outbox() == 1
    assert hist.call_count == 1
    assert await repo.get_outbox() == []


@pytest.mark.asyncio
@respx.mock
async def test_unwatch_and_set_count_push_removals(env):
    repo, client, sync = env
    mock_reads()
    await sync.sync(deep=False)
    sev = await repo.get_show_by_external_id("simkl", 1400298)
    await sync.unmark_last_episode(sev.id)
    out = await repo.get_due_outbox("simkl")
    assert out[-1]["op"] == "unwatched"
    assert simkl_body(out[-1]["operation"])["shows"][0]["seasons"] == [{"number": 1, "episodes": [{"number": 2}]}]

    updated = await sync.set_watched_count(sev.id, 3)
    assert updated.watched_episodes == 3
    adds = [o for o in await repo.get_due_outbox("simkl") if o["op"] == "watched"]
    nums = sorted(e["number"] for e in simkl_body(adds[-1]["operation"])["shows"][0]["seasons"][0]["episodes"])
    assert nums == [2, 3]


@pytest.mark.asyncio
@respx.mock
async def test_pending_local_change_is_not_overwritten_by_pull(env):
    repo, client, sync = env
    mock_reads()
    await sync.sync(deep=False)
    sev = await repo.get_show_by_external_id("simkl", 1400298)
    await sync.update_show_status(sev.id, ShowStatus.HOLD)
    # SIMKL is down for writes: push fails, pull still says "watching"
    respx.post(f"{SIMKL_BASE_URL}/sync/add-to-list").respond(status_code=503)
    await sync.sync(force_full=True, deep=False)
    sev = await repo.get_show_by_external_id("simkl", 1400298)
    assert sev.status == ShowStatus.HOLD
    outbox = await repo.get_outbox()
    assert len(outbox) == 1 and outbox[0]["attempts"] == 1 and outbox[0]["last_error"]


@pytest.mark.asyncio
@respx.mock
async def test_delta_pull_uses_date_from_and_detects_removals(env):
    repo, client, sync = env
    mock_reads()
    await sync.sync(deep=False)

    acts2 = json.loads(json.dumps(ACTIVITIES_1))
    acts2["all"] = "2026-10-05T10:00:00Z"
    acts2["movies"]["removed_from_list"] = "2026-10-05T10:00:00Z"
    acts2["tv_shows"]["watching"] = "2026-10-05T10:00:00Z"
    acts2["tv_shows"]["all"] = "2026-10-05T10:00:00Z"
    respx.get(f"{SIMKL_BASE_URL}/sync/activities").respond(json=acts2)
    changed = dict(SEVERANCE, watched_episodes_count=3,
                   seasons=[{"number": 1, "episodes": [{"number": n} for n in (1, 2, 3)]}])
    shows_route = respx.get(f"{SIMKL_BASE_URL}/sync/all-items/shows").respond(json={"shows": [changed]})
    respx.get(f"{SIMKL_BASE_URL}/sync/all-items/movies").respond(json={"movies": []})

    result = await sync.sync(deep=False)
    assert result["mode"] == "delta"
    assert shows_route.calls.last.request.url.params["date_from"] == ACTIVITIES_1["all"]
    assert (await repo.get_show_by_external_id("simkl", 1400298)).watched_episodes == 3
    assert await repo.get_show_by_external_id("simkl", 2222) is None  # removed on SIMKL


@pytest.mark.asyncio
@respx.mock
async def test_rating_and_remove_payloads(env):
    repo, client, sync = env
    mock_reads()
    await sync.sync(deep=False)
    movie = await repo.get_show_by_external_id("simkl", 2222)
    await sync.update_show_rating(movie.id, 7.6)
    await sync.update_show_rating(movie.id, None)
    await sync.remove_from_library(movie)
    ops = [(o["op"], simkl_body(o["operation"])) for o in await repo.get_due_outbox("simkl")]
    assert ops[0] == ("rate", {"movies": [{"ids": {"simkl": 2222, "tmdb": 693134}, "rating": 8}]})
    assert ops[1][0] == "unrate"
    assert ops[2][0] == "remove" and "movies" in ops[2][1]

    rs = respx.post(f"{SIMKL_BASE_URL}/sync/ratings").respond(json={})
    rr = respx.post(f"{SIMKL_BASE_URL}/sync/ratings/remove").respond(json={})
    hr = respx.post(f"{SIMKL_BASE_URL}/sync/history/remove").respond(json={})
    assert await sync.flush_outbox() == 3
    assert rs.called and rr.called and hr.called


@pytest.mark.asyncio
@respx.mock
async def test_revoked_token_reported(env):
    repo, client, sync = env
    respx.get(f"{SIMKL_BASE_URL}/sync/activities").respond(status_code=401, json={"error": "user_token_failed"})
    result = await sync.sync()
    assert result["status"] == "error"
    assert "connect SIMKL" in await repo.get_state("driver.simkl.last_error")


@pytest.mark.asyncio
async def test_season_step_without_episode_list_uses_estimate(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "s.db"))
    await repo.initialize([])
    sync = simkl_engine(SimklClient(client_id=""), repo)
    from penguwatch.models import Show
    sid = await repo.upsert_show(Show(title="Local Show", total_episodes=25, aired_episodes=25))
    assert (await sync.step_season(sid, 1)).watched_episodes == 10
    assert (await sync.step_season(sid, 2)).watched_episodes == 25
    assert (await sync.step_season(sid, -1)).watched_episodes == 20
    await repo.close()


@pytest.mark.asyncio
@respx.mock
async def test_token_sent_on_catalogue_calls_and_expired_token_refresh(env):
    repo, client, sync = env
    route = respx.get(f"{SIMKL_BASE_URL}/search/tv").respond(json=[])
    await client.search("x", kinds=("tv",), enrich=0)
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"

    client.refresh_token = "ref"
    saved = {}

    async def persist(a, r, e=None):
        saved.update(a=a, r=r)

    client.on_token_refreshed = persist
    respx.get(f"{SIMKL_BASE_URL}/sync/activities").mock(side_effect=[
        httpx.Response(401, json={"error": "invalid_token", "message": "Access token has expired"}),
        httpx.Response(200, json={"all": None}),
    ])
    respx.post(f"{SIMKL_BASE_URL}/oauth2/token").respond(json={"access_token": "new", "refresh_token": "ref2"})
    assert await client.get_activities() == {"all": None}
    assert client.access_token == "new" and saved == {"a": "new", "r": "ref2"}
