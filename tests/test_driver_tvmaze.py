"""The TVmaze driver (docs/drivers.md): recorded answers (tests/data/drivers/tvmaze/) through respx, never the network.

The files are real answers of api.tvmaze.com, trimmed to what the tests read (fewer episodes and alternative names);
show_next_episode.json is the one hand-written case (the recorded show had no upcoming episode when recorded).
"""

import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from penguwatch.api.deps import AppContainer, install
from penguwatch.config import AppConfig
from penguwatch.db.repository import DatabaseRepository
from penguwatch.drivers import DRIVERS, drivers_with
from penguwatch.drivers.base import Capability
from penguwatch.drivers.ratelimit import RateLimiter
from penguwatch.drivers.tvmaze import mapper
from penguwatch.drivers.tvmaze.client import BASE_URL, TvmazeClient, TvmazeError
from penguwatch.drivers.tvmaze.options import TvmazeOptions
from penguwatch.drivers.tvmaze.service import TvmazeService
from penguwatch.main import app, security_config
from penguwatch.models import MediaType, Show, ShowStatus
from penguwatch.optimizer.engine import SubscriptionOptimizer
from penguwatch.ratings import stored_builtin_specs
from penguwatch.sync.active import ActiveDrivers
from penguwatch.sync.engine import SyncEngine
from tests.simkl_helpers import simkl_service

DATA = Path(__file__).parent / "data" / "drivers" / "tvmaze"


def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def no_network():
    """Every test runs inside respx: a request nobody mocked fails instead of reaching the network."""
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        yield mock


class Clock:
    """A fake clock whose sleep advances it."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
async def service(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "t.db"))
    await repo.initialize([])
    clock = Clock()
    client = TvmazeClient(app_version="9.9", retry_base_delay=2.0, sleep=clock.sleep,
                          limiter=RateLimiter(18, 10.0, clock=clock, sleep=clock.sleep))
    svc = TvmazeService(client, repo, TvmazeOptions())
    svc.clock = clock
    yield svc
    await svc.aclose()
    await repo.close()


# --- the registry --------------------------------------------------------------------------------------------------

def test_the_driver_is_registered_with_what_it_offers():
    driver = DRIVERS["tvmaze"]
    assert driver.offers == {Capability.CATALOG, Capability.SCHEDULE, Capability.RATINGS}
    assert driver.health_url.startswith("https://api.tvmaze.com")
    assert driver.attribution.text and driver.attribution.url == "https://www.tvmaze.com"
    assert DRIVERS["tvmaze"] in drivers_with(Capability.SCHEDULE)
    assert DRIVERS["tvmaze"] not in drivers_with(Capability.DISCOVERY)  # no trending or recommendations
    spec, = driver.rating_sources
    assert spec.column is None and spec.id == "tvmaze"
    assert (spec.scale_min, spec.scale_max) == (0, 10)


def test_simkl_stays_the_default():
    cfg = AppConfig().drivers
    assert cfg.library == cfg.catalog[0] == cfg.schedule[0] == cfg.discovery[0] == "simkl"
    assert AppConfig(drivers={"catalog": ["tvmaze", "simkl"]}).drivers.catalog == ["tvmaze", "simkl"]
    with pytest.raises(ValueError, match="does not offer discovery"):
        AppConfig(drivers={"discovery": ["tvmaze"]})
    with pytest.raises(ValueError, match="does not offer library_sync"):
        AppConfig(drivers={"library": "tvmaze"})


# --- mapping ----------------------------------------------------------------------------------------------------

def test_a_show_maps_to_a_title_with_ids_and_a_rating():
    t = mapper.show_title(load("show_44933.json"))
    assert t.media_type == "show" and t.title == "Severance" and t.year == 2022
    assert t.ids == {"tvmaze": 44933, "imdb": "tt11280740", "tvdb": 371980}
    assert t.network == "Apple TV"  # a web channel when there is no network
    assert t.runtime == 49 and t.status == "running"  # averageRuntime when runtime is null
    assert t.genres == ["Drama", "Science-Fiction", "Mystery"]
    assert t.overview.startswith("Mark Scout leads a team") and "<" not in t.overview
    assert t.poster.startswith("https://static.tvmaze.com/") and t.poster.endswith(".jpg")
    assert t.ratings["tvmaze"].value == 7.7 and not t.ratings["tvmaze"].fallback
    assert t.extra["url"] == "https://www.tvmaze.com/shows/44933/severance"
    assert t.alt_titles is None  # not asked until akas are given


def test_anime_is_recognised_by_its_genre_and_ended_is_ended():
    show = load("show_1121.json")
    t = mapper.show_title(show, akas=show["_embedded"]["akas"])
    assert t.media_type == "anime" and t.status == "ended"
    assert t.ids["tvmaze"] == 1121 and t.ids["imdb"] == "tt0213338"
    assert t.network == "WOWOW"
    assert [a["name"] for a in t.alt_titles] and all(isinstance(a["name"], str) for a in t.alt_titles)


def test_missing_fields_stay_empty():
    t = mapper.show_title({"id": 5, "name": "Bare", "externals": None, "rating": {"average": None}, "image": None,
                           "network": None, "webChannel": None, "summary": None, "premiered": None,
                           "genres": None, "runtime": None, "averageRuntime": None, "status": None})
    assert t.ids == {"tvmaze": 5} and t.year is None and t.poster is None and t.overview is None
    assert t.network is None and t.runtime is None and t.status is None and t.ratings == {} and t.genres == []
    assert mapper.show_title({"id": 5}) is None and mapper.show_title({"name": "No id"}) is None
    assert mapper.show_title({"id": 5, "name": "Zero", "rating": {"average": 0}}).ratings == {}


def test_episodes_are_numbered_in_airing_order_and_unnumbered_specials_are_left_out():
    eps = mapper.parse_episodes(load("episodes_44933.json"))
    assert [(e.season, e.number) for e in eps] == [(1, 1), (1, 2), (1, 3), (1, 4), (2, 10)]
    assert eps[0].title == "Good News About Hell" and eps[0].air_date == "2022-02-18"
    assert eps[0].ids == {"tvmaze": 2238231}
    raw = [{"id": 1, "season": 1, "number": None, "airdate": "2020-01-01", "name": "Special"},
           {"id": 2, "season": 2, "number": 1, "airdate": "", "name": ""}, "junk", {"id": 3}]
    got = mapper.parse_episodes(raw)
    assert [(e.season, e.number, e.air_date, e.title) for e in got] == [(2, 1, None, None)]
    assert mapper.parse_episodes(None) == []


def test_the_next_episode_of_a_show_record():
    show = load("show_next_episode.json")
    entry = mapper.next_episode(show)
    assert (entry.ids, entry.season, entry.number, entry.air_date, entry.title) == (
        {"tvmaze": 44933}, 3, 1, "2027-03-05", "Next Up")
    assert mapper.next_episode(load("show_44933.json")) is None  # nothing embedded
    show["_embedded"]["nextepisode"]["airdate"] = ""
    assert mapper.next_episode(show) is None


# --- the catalogue -----------------------------------------------------------------------------------------------

async def test_search_returns_titles_best_first(service, no_network):
    route = no_network.get(f"{BASE_URL}/search/shows").respond(json=load("search_severance.json"))
    found = await service.catalog.search("severance", ["show", "anime"], limit=2)
    assert [t.ids["tvmaze"] for t in found] == [44933, 85532]
    assert route.calls.last.request.url.params["q"] == "severance"
    assert len(await service.catalog.search("severance", ["show"], limit=1)) == 1


async def test_search_filters_by_the_type_asked_for(service, no_network):
    no_network.get(f"{BASE_URL}/search/shows").respond(json=load("search_anime.json") + load("search_severance.json")[:1])
    assert [t.title for t in await service.catalog.search("x", ["anime"])] == ["Cowboy Bebop"]
    assert [t.title for t in await service.catalog.search("x", ["show"])] == ["Severance"]
    calls = len(no_network.calls)
    assert await service.catalog.search("x", ["movie"]) == []  # TVmaze has no movies: no request at all
    assert len(no_network.calls) == calls


async def test_search_survives_odd_answers(service, no_network):
    no_network.get(f"{BASE_URL}/search/shows").respond(json=[{"score": 1}, "junk", {"show": {"id": 1}}])
    assert await service.catalog.search("x", ["show"]) == []
    no_network.get(f"{BASE_URL}/search/shows").respond(json={"not": "a list"})
    assert await service.catalog.search("x", ["show"]) == []


async def test_title_fetches_details_with_the_alternative_names(service, no_network):
    route = no_network.get(f"{BASE_URL}/shows/44933").respond(json=load("show_44933.json"))
    t = await service.catalog.title("show", {"tvmaze": 44933})
    assert t.title == "Severance" and len(t.alt_titles) == 3
    assert route.calls.last.request.url.params["embed[]"] == "akas"
    assert await service.catalog.title("show", {"imdb": "tt1"}) is None  # not its id: no request


async def test_title_for_a_movie_or_an_unknown_show_is_none(service, no_network):
    gone = no_network.get(f"{BASE_URL}/shows/9").respond(404)
    assert await service.catalog.title("movie", {"tvmaze": 44933}) is None
    assert await service.catalog.title(None, {"tvmaze": 9}) is None
    assert gone.call_count == 1


async def test_episodes(service, no_network):
    no_network.get(f"{BASE_URL}/shows/44933/episodes").respond(json=load("episodes_44933.json"))
    eps = await service.catalog.episodes("show", {"tvmaze": 44933})
    assert len(eps) == 5
    no_network.get(f"{BASE_URL}/shows/7/episodes").respond(404)
    assert await service.catalog.episodes("show", {"tvmaze": 7}) == []
    assert await service.catalog.episodes("show", {"imdb": "tt1"}) is None
    assert await service.catalog.related("anime", {"tvmaze": 44933}) == []


async def test_resolve_by_imdb_follows_the_redirect(service, no_network):
    no_network.get(f"{BASE_URL}/lookup/shows").respond(
        301, headers={"Location": f"{BASE_URL}/shows/44933"})
    no_network.get(f"{BASE_URL}/shows/44933").respond(json=load("lookup_imdb.json"))
    ids, media = await service.catalog.resolve("show", {"imdb": "tt11280740", "tmdb": 95396})
    assert ids == {"imdb": "tt11280740", "tmdb": 95396, "tvmaze": 44933, "tvdb": 371980} and media == "show"
    lookup = [c for c in no_network.calls if c.request.url.path == "/lookup/shows"][0]
    assert dict(lookup.request.url.params) == {"imdb": "tt11280740"}


async def test_resolve_by_tvdb_nothing_found_and_movies(service, no_network):
    route = no_network.get(f"{BASE_URL}/lookup/shows").respond(404)
    assert await service.catalog.resolve("show", {"tvdb": 123}) is None
    assert dict(route.calls.last.request.url.params) == {"thetvdb": "123"}
    assert await service.catalog.resolve("show", {"tmdb": 5}) is None  # no id TVmaze can look up
    assert await service.catalog.resolve("movie", {"imdb": "tt1"}) is None
    assert await service.catalog.resolve("show", {"tvmaze": 4}) == ({"tvmaze": 4}, "show")


# --- the schedule ---------------------------------------------------------------------------------------------------

async def add_show(repo, title, tvmaze, **fields):
    return await repo.upsert_show(Show(title=title, ids={"tvmaze": tvmaze}, media_type=MediaType.SHOW,
                                       status=ShowStatus.WATCHING, **fields))


async def test_schedule_checks_running_library_titles_only(service, no_network):
    repo = service.repository
    await add_show(repo, "Severance", 44933)
    await add_show(repo, "Over", 8, is_ended=True)
    await repo.upsert_show(Show(title="No id", ids={"imdb": "tt1"}, media_type=MediaType.SHOW))
    route = no_network.get(f"{BASE_URL}/shows/44933").respond(json=load("show_next_episode.json"))
    got = await service.schedule.upcoming()
    assert [(e.ids, e.season, e.number, e.air_date) for e in got] == [({"tvmaze": 44933}, 3, 1, "2027-03-05")]
    assert route.call_count == 1 and route.calls.last.request.url.params["embed"] == "nextepisode"


async def test_schedule_gives_up_when_tvmaze_keeps_failing(service, no_network):
    for n in range(1, 7):
        await add_show(service.repository, f"S{n}", n)
    service.client.retries = 0
    route = no_network.get(path__regex=r"^/shows/\d+$").respond(500)
    assert await service.schedule.upcoming() == []
    assert route.call_count == 3  # not one request per title


async def test_schedule_caps_the_titles_per_run(service, no_network):
    service.options.schedule_max_titles = 2
    for n in range(1, 5):
        await add_show(service.repository, f"S{n}", n)
    route = no_network.get(path__regex=r"^/shows/\d+$").respond(404)
    assert await service.schedule.upcoming() == []
    assert route.call_count == 2


# --- the client ---------------------------------------------------------------------------------------------------

async def test_requests_carry_a_descriptive_user_agent_and_report_telemetry(service, no_network):
    seen = []
    service.client.on_request = lambda *a: seen.append(a)
    route = no_network.get(f"{BASE_URL}/shows/1").respond(json={"id": 1})
    await service.client.get("/shows/1")
    ua = route.calls.last.request.headers["user-agent"]
    assert ua.startswith("PenguWatch/9.9 ") and "tvmaze.com" in ua
    (svc, path, status, seconds), = seen
    assert (svc, path, status) == ("tvmaze", "/shows/1", 200) and seconds >= 0


async def test_a_telemetry_callback_that_fails_does_not_break_the_call(service, no_network):
    def boom(*a):
        raise RuntimeError("telemetry down")
    service.client.on_request = boom
    no_network.get(f"{BASE_URL}/shows/1").respond(json={"id": 1})
    assert await service.client.get("/shows/1") == {"id": 1}


async def test_429_is_retried_after_the_pause_tvmaze_asks_for(service, no_network):
    route = no_network.get(f"{BASE_URL}/shows/1")
    route.side_effect = [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={"id": 1})]
    assert await service.client.get("/shows/1") == {"id": 1}
    assert route.call_count == 2 and 7.0 in service.clock.slept


async def test_429_without_retry_after_backs_off_and_a_huge_one_is_capped(service, no_network):
    route = no_network.get(f"{BASE_URL}/shows/1")
    route.side_effect = [httpx.Response(429), httpx.Response(503, headers={"Retry-After": "9999"}),
                         httpx.Response(200, json={"id": 1})]
    await service.client.get("/shows/1")
    assert service.clock.slept == [2.0, 30.0]  # retry_base_delay * attempt, then the cap


async def test_a_429_that_keeps_coming_ends_in_an_error(service, no_network):
    route = no_network.get(f"{BASE_URL}/shows/1").respond(429)
    with pytest.raises(TvmazeError) as err:
        await service.client.get("/shows/1")
    assert err.value.status_code == 429 and route.call_count == 3  # the first try and two retries


async def test_other_errors_are_not_retried_and_a_404_is_an_error_unless_allowed(service, no_network):
    route = no_network.get(f"{BASE_URL}/shows/1").respond(404)
    with pytest.raises(TvmazeError) as err:
        await service.client.get("/shows/1")
    assert err.value.status_code == 404 and route.call_count == 1
    assert await service.client.get("/shows/1", missing_ok=True) is None
    no_network.get(f"{BASE_URL}/shows/2").respond(400)
    with pytest.raises(TvmazeError):
        await service.client.get("/shows/2", missing_ok=True)


async def test_network_errors_are_retried_then_reported_with_status_0(service, no_network):
    seen = []
    service.client.on_request = lambda *a: seen.append(a[2])
    route = no_network.get(f"{BASE_URL}/shows/1").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(TvmazeError) as err:
        await service.client.get("/shows/1")
    assert err.value.status_code == 0 and route.call_count == 3 and seen == [0, 0, 0]


async def test_an_answer_that_is_not_json_is_an_error(service, no_network):
    no_network.get(f"{BASE_URL}/shows/1").respond(200, text="<html>")
    with pytest.raises(TvmazeError, match="not JSON"):
        await service.client.get("/shows/1")


# --- the rate limiter ---------------------------------------------------------------------------------------------

async def test_the_limiter_lets_a_burst_through_then_waits_for_the_window():
    clock = Clock()
    limiter = RateLimiter(3, 10.0, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        await limiter.wait()
    assert clock.slept == []
    await limiter.wait()  # the fourth waits until the first has left the window
    assert clock.slept == [10.0]
    clock.now += 4
    await limiter.wait()
    await limiter.wait()  # three in the window now (one at +10, two at +14): still room
    assert clock.slept == [10.0]
    await limiter.wait()  # the fourth waits for the first of them to leave
    assert clock.slept == [10.0, 6.0]


async def test_the_limiter_never_allows_more_than_its_calls_in_any_window():
    clock = Clock()
    limiter = RateLimiter(5, 10.0, clock=clock, sleep=clock.sleep)
    starts = []
    for _ in range(23):
        await limiter.wait()
        starts.append(clock.now)
    for i, t in enumerate(starts):
        assert sum(1 for s in starts if t <= s < t + 10.0) <= 5
    assert starts[-1] - starts[0] >= 40.0


async def test_concurrent_callers_share_the_limit():
    clock = Clock()
    limiter = RateLimiter(2, 10.0, clock=clock, sleep=clock.sleep)
    done = []

    async def one(n):
        await limiter.wait()
        done.append((n, clock.now))

    await asyncio.gather(*(one(n) for n in range(6)))
    assert sorted(t for _, t in done) == [1000.0, 1000.0, 1010.0, 1010.0, 1020.0, 1020.0]


def test_the_limiter_refuses_nonsense():
    with pytest.raises(ValueError):
        RateLimiter(0, 10)
    with pytest.raises(ValueError):
        RateLimiter(1, 0)


async def test_the_client_waits_on_its_limiter_before_every_request(service, no_network):
    service.client.limiter = RateLimiter(2, 10.0, clock=service.clock, sleep=service.clock.sleep)
    no_network.get(path__regex=r"^/shows/\d$").respond(json={"id": 1})
    for n in range(3):
        await service.client.get(f"/shows/{n}")
    assert service.clock.slept == [10.0]


def test_the_default_limit_is_under_the_published_one():
    o = TvmazeOptions()
    assert o.rate_limit_calls / o.rate_window_seconds < 20 / 10  # TVmaze: at least 20 calls per 10 seconds


# --- ratings stored without a column -----------------------------------------------------------------------------

@pytest.fixture
async def chain(tmp_path):
    """The library driver (SIMKL, never connected) with TVmaze as the catalogue and schedule, as a user would set it
    up with `catalog: [tvmaze]`, around an installed app container."""
    config = AppConfig(drivers={"catalog": ["tvmaze"], "schedule": ["tvmaze"]})
    config.database.db_path = str(tmp_path / "chain.db")
    repo = DatabaseRepository(config.database.db_path)
    await repo.initialize([])
    await repo.seed_builtin_rating_sources(stored_builtin_specs(["simkl", "tvmaze"]))
    clock = Clock()
    tvmaze = TvmazeService(
        TvmazeClient(app_version="1", sleep=clock.sleep, limiter=RateLimiter(18, 10.0, clock=clock, sleep=clock.sleep)),
        repo, TvmazeOptions())
    simkl = simkl_service(None, repo)  # nobody connected: no client is ever used
    simkl.client = type("Unconnected", (), {"access_token": None, "client_id": "", "refresh_token": None})()
    engine = SyncEngine(library=simkl.library, repository=repo, catalogs=[tvmaze.catalog], schedules=[tvmaze.schedule])
    drivers = ActiveDrivers(by_name={"simkl": simkl, "tvmaze": tvmaze}, library=simkl.library,
                            catalogs=[tvmaze.catalog], schedules=[tvmaze.schedule])
    container = AppContainer(config=config, repository=repo, drivers=drivers, sync=engine,
                             optimizer=SubscriptionOptimizer(repository=repo, config=config.optimizer),
                             security=security_config, notifier=None)
    install(app, container)
    yield container
    await tvmaze.aclose()
    await repo.close()
    del app.state.container


def http():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def mock_show(mock, name, tvmaze_id, episodes=None):
    mock.get(f"{BASE_URL}/shows/{tvmaze_id}").respond(json=load(name))
    mock.get(f"{BASE_URL}/shows/{tvmaze_id}/episodes").respond(json=load(episodes))


async def test_the_chain_adds_a_tv_show_and_an_anime_and_fills_episodes_and_ratings(chain, no_network):
    no_network.get(f"{BASE_URL}/search/shows", params={"q": "Severance"}).respond(json=load("search_severance.json"))
    no_network.get(f"{BASE_URL}/search/shows", params={"q": "Cowboy Bebop"}).respond(json=load("search_anime.json"))
    mock_show(no_network, "show_44933.json", 44933, "episodes_44933.json")
    mock_show(no_network, "show_1121.json", 1121, "episodes_1121.json")
    async with http() as ac:
        shown = (await ac.get("/api/catalog/search", params={"q": "Severance", "type": "tv"})).json()
        assert [i["ids"]["tvmaze"] for i in shown] == [44933, 85532]
        assert shown[0]["ratings"] == {"tvmaze": {"rating": 7.7, "votes": None}} and shown[0]["in_library"] is False

        ids = {}
        for name, kind in (("Severance", "show"), ("Cowboy Bebop", "anime")):
            created = (await ac.post("/api/shows", json={"title": name, "media_type": kind, "status": "plantowatch"}))
            assert created.status_code == 200, created.text
            ids[name] = created.json()
        await asyncio.gather(*list(chain.background))  # the episode lists, fetched after the add

        assert ids["Severance"]["media_type"] == "show" and ids["Cowboy Bebop"]["media_type"] == "anime"
        assert ids["Severance"]["ids"]["tvmaze"] == 44933
        sev = (await ac.get(f"/api/shows/{ids['Severance']['id']}")).json()
        assert sev["total_episodes"] >= 0 and sev["network"] == "Apple TV"

        # the add stored the rating; enrichment (the sync engine's job) fills cross ids, artwork and genres
        await chain.sync.enrich_titles([s["id"] for s in ids.values()], pace=False)
        for name, imdb, rating in (("Severance", "tt11280740", 7.7), ("Cowboy Bebop", "tt0213338", 8.3)):
            show = (await ac.get(f"/api/shows/{ids[name]['id']}")).json()
            assert show["ids"]["imdb"] == imdb and show["poster"] and show["genres"]
            mine = {r["source"]: r for r in (await ac.get(f"/api/shows/{show['id']}/ratings")).json()}["tvmaze"]
            assert mine["value"] == rating and mine["builtin"] is True and mine["scale_max"] == 10
            assert mine["url"].startswith("https://www.tvmaze.com/shows/")
        eps = await chain.repository.get_episodes(ids["Severance"]["id"])
        assert [(e.season, e.number) for e in eps] == [(1, 1), (1, 2), (1, 3), (1, 4), (2, 10)]
        anime_eps = await chain.repository.get_episodes(ids["Cowboy Bebop"]["id"])
        assert len(anime_eps) == 3 and anime_eps[0].air_date
    assert chain.sync.library.is_connected() is False  # the account-free path never needed a login
    assert not any(c.request.url.host != "api.tvmaze.com" for c in no_network.calls)


async def test_the_rating_source_is_builtin_read_only_and_usable_for_sorting(chain, no_network):
    mock_show(no_network, "show_44933.json", 44933, "episodes_44933.json")
    mock_show(no_network, "show_1121.json", 1121, "episodes_1121.json")
    repo = chain.repository
    a = await add_show(repo, "Severance", 44933)
    b = await add_show(repo, "Cowboy Bebop", 1121)
    await chain.sync.enrich_titles([a, b], pace=False)
    async with http() as ac:
        sources = {s["id"]: s for s in (await ac.get("/api/rating-sources")).json()}
        assert list(sources)[:3] == ["simkl", "imdb", "tvmaze"]
        s = sources["tvmaze"]
        assert s["builtin"] is True and s["read_only"] is True and s["origin"] == "builtin" and s["rated_titles"] == 2
        assert (await ac.patch("/api/rating-sources/tvmaze", json={"name": "x"})).status_code == 409
        assert (await ac.delete("/api/rating-sources/tvmaze")).status_code == 409
        put = await ac.put("/api/ratings/tvmaze", json={"items": [{"match": {"imdb": "tt11280740"}, "value": 1}]})
        assert put.status_code == 409
        clash = await ac.post("/api/rating-sources", json={"id": "tvmaze", "name": "Mine", "label": "M"})
        assert clash.status_code == 422  # the id is reserved
        titles = [x["title"] for x in (await ac.get("/api/shows", params={"sort_by": "rating:tvmaze"})).json()]
        assert titles == ["Cowboy Bebop", "Severance"]  # 8.3 before 7.7
        best = [x["title"] for x in (await ac.get("/api/shows", params={"min_rating": "tvmaze:8"})).json()]
        assert best == ["Cowboy Bebop"]


async def test_custom_sources_still_count_apart_from_the_builtin_rows(chain):
    from penguwatch.db.ratings import MAX_SOURCES
    from penguwatch.ratings import RatingSourceSpec
    for n in range(MAX_SOURCES):
        await chain.repository.create_rating_source(RatingSourceSpec(id=f"c{n}", name="C", label="C"))
    with pytest.raises(ValueError, match="at most"):
        await chain.repository.create_rating_source(RatingSourceSpec(id="more", name="C", label="C"))


async def test_a_source_nobody_seeded_is_skipped_quietly(chain, no_network, tmp_path):
    async with chain.repository.write() as db:
        await db.execute("DELETE FROM rating_sources")
    mock_show(no_network, "show_44933.json", 44933, "episodes_44933.json")
    a = await add_show(chain.repository, "Severance", 44933)
    assert await chain.sync.enrich_titles([a], pace=False) == 1  # the other fields are still filled
    assert (await chain.repository.get_show_by_id(a)).enriched_at


async def test_a_refresh_with_a_changed_rating_updates_it_without_a_rating_event(chain, no_network):
    events = []
    chain.repository._event_listeners.append(lambda *a, **k: events.append(a))
    mock_show(no_network, "show_44933.json", 44933, "episodes_44933.json")
    a = await add_show(chain.repository, "Severance", 44933)
    await chain.sync.enrich_titles([a], pace=False)
    newer = copy.deepcopy(load("show_44933.json"))
    newer["rating"]["average"] = 8.4
    no_network.get(f"{BASE_URL}/shows/44933").respond(json=newer)
    await chain.repository.update_show_fields(a, {"network": None})  # makes it due again
    await chain.sync.enrich_titles([a], pace=False)
    assert (await chain.repository.get_title_ratings(a))["tvmaze"].value == 8.4
    assert not [e for e in events if "rating" in e[0]]


async def test_the_drivers_listing_carries_the_attribution(chain):
    async with http() as ac:
        listing = {d["name"]: d for d in (await ac.get("/api/drivers")).json()["drivers"]}
    assert listing["tvmaze"]["attribution"] == {"text": "TV data from TVmaze", "url": "https://www.tvmaze.com"}
    assert listing["simkl"]["attribution"] is None
    assert listing["tvmaze"]["offers"] == ["catalog", "ratings", "schedule"]


async def test_the_schedule_feeds_titles_without_an_episode_list(chain, no_network):
    a = await add_show(chain.repository, "Severance", 44933)
    no_network.get(f"{BASE_URL}/shows/44933").respond(json=load("show_next_episode.json"))
    assert await chain.sync.sync_upcoming_schedule() == 1
    show = await chain.repository.get_show_by_id(a)
    assert (show.next_episode_air_date, show.next_episode_season, show.next_episode_number) == ("2027-03-05", 3, 1)


async def test_F2_a_custom_source_with_the_drivers_id_is_left_alone_and_reported(chain, no_network, caplog):
    """A custom source made on an older version, before the id was reserved: turning the driver on must neither turn
    it into the built-in one nor overwrite its values with the driver's ratings."""
    mock_show(no_network, "show_44933.json", 44933, "episodes_44933.json")
    repo = chain.repository
    show_id = await add_show(repo, "Severance", 44933)
    async with repo.write() as db:  # the state of a database from before the upgrade
        await db.execute("DELETE FROM rating_sources WHERE id = 'tvmaze'")
        await db.execute("INSERT INTO rating_sources (id, name, label, scale_min, scale_max, decimals, origin) "
                         "VALUES ('tvmaze', 'My TV score', 'Mine', 0, 100, 0, 'api')")
        await db.execute("INSERT INTO title_ratings (show_id, source_id, value) VALUES (?, 'tvmaze', 85)", (show_id,))

    with caplog.at_level("WARNING", logger="penguwatch.db"):
        await repo.seed_builtin_rating_sources(stored_builtin_specs(["simkl", "tvmaze"]))  # the next start-up
    assert any("'tvmaze'" in r.message and "rename or delete" in r.message for r in caplog.records)
    source = next(s for s in await repo.get_rating_sources() if s.id == "tvmaze")
    assert (source.name, source.label, source.scale_max, source.origin, source.builtin) == ("My TV score", "Mine", 100, "api", False)

    await chain.sync.enrich_titles([show_id], pace=False)  # the driver now has a rating for the title: not stored
    db = await repo.read_connection()
    values = await (await db.execute("SELECT value FROM title_ratings WHERE source_id = 'tvmaze'")).fetchall()
    assert [v[0] for v in values] == [85]

    async with http() as ac:
        warnings = (await ac.get("/api/system")).json()["warnings"]
    assert len(warnings) == 1 and "'tvmaze'" in warnings[0] and "rename or delete" in warnings[0]


async def test_F2_without_a_conflict_the_system_tab_has_no_warning_and_the_ratings_are_stored(chain, no_network):
    mock_show(no_network, "show_44933.json", 44933, "episodes_44933.json")
    show_id = await add_show(chain.repository, "Severance", 44933)
    await chain.sync.enrich_titles([show_id], pace=False)
    db = await chain.repository.read_connection()
    assert (await (await db.execute("SELECT COUNT(*) FROM title_ratings WHERE source_id = 'tvmaze'")).fetchone())[0] == 1
    async with http() as ac:
        assert (await ac.get("/api/system")).json()["warnings"] == []


async def test_schedule_takes_turns_so_every_running_title_is_reached(service, no_network):
    service.options.schedule_max_titles = 3
    ids = {}
    for n in range(1, 9):  # 8 running titles, 3 per run; TVmaze ids differ from the library ids
        ids[await add_show(service.repository, f"S{n}", 100 + n)] = 100 + n
    await add_show(service.repository, "Over", 999, is_ended=True)  # never checked
    asked = []
    no_network.get(path__regex=r"^/shows/\d+$").mock(
        side_effect=lambda r: (asked.append(int(r.url.path.split("/")[-1])), httpx.Response(404))[1])
    runs = []
    for _ in range(4):
        before = len(asked)
        assert await service.schedule.upcoming() == []
        runs.append(asked[before:])
    assert [len(r) for r in runs] == [3, 3, 3, 3]
    assert runs[0] == [101, 102, 103] and runs[1] == [104, 105, 106] and runs[2] == [107, 108, 101]  # wraps round
    assert set(asked) == {101, 102, 103, 104, 105, 106, 107, 108}  # all reached within three runs
    assert 999 not in asked
    assert await service.repository.get_state("driver.tvmaze.schedule_cursor") == str(sorted(ids)[3])  # S4, the last of run 4


async def test_schedule_keeps_no_cursor_while_every_title_fits(service, no_network):
    service.options.schedule_max_titles = 4
    for n in range(1, 4):
        await add_show(service.repository, f"S{n}", n)
    no_network.get(path__regex=r"^/shows/\d+$").respond(404)
    await service.schedule.upcoming()
    assert await service.repository.get_state("driver.tvmaze.schedule_cursor") is None


async def test_schedule_that_gives_up_tries_the_same_titles_next_time(service, no_network):
    service.options.schedule_max_titles = 5
    service.client.retries = 0
    for n in range(1, 8):
        await add_show(service.repository, f"S{n}", n)
    no_network.get(path__regex=r"^/shows/\d+$").respond(500)
    await service.schedule.upcoming()
    assert await service.repository.get_state("driver.tvmaze.schedule_cursor") is None


async def test_search_lists_an_exact_match_far_down_first(service, no_network):
    hits = [{"score": 1 - i / 100, "show": {**load("show_44933.json"), "id": 1000 + i, "name": f"Severance Spinoff {i}"}}
            for i in range(25)]
    hits[20]["show"].update(id=44933, name="Severance")
    no_network.get(f"{BASE_URL}/search/shows").respond(json=hits)
    found = await service.catalog.search("Severance", ["show"])
    assert found[0].ids["tvmaze"] == 44933 and len(found) == 25
