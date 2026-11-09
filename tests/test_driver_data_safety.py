"""Data-safety fixes of the driver layer: a push that raises anything must not block the outbox, the library driver's
catalogue owns the episode numbering of its titles, and a title added before the account is connected is kept."""

from types import SimpleNamespace

import httpx
import pytest
import respx

from penguwatch.config import AppConfig
from penguwatch.db.repository import DatabaseRepository
from penguwatch.drivers import identity_providers
from penguwatch.drivers.base import Episode, Title
from penguwatch.drivers.simkl.client import SIMKL_BASE_URL, SimklClient
from penguwatch.models import MediaType, ServiceConfig, Show, ShowCreateRequest, ShowStatus
from tests.simkl_helpers import simkl_engine
from tests.test_two_way_sync import MOVIE, SEVERANCE, mock_reads


@pytest.fixture
async def env(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "safety.db"), key_providers=("simkl",))
    await repo.initialize([ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99, aliases=["Apple TV+"])])
    await repo.set_state("sync_review_mode", "off")
    client = SimklClient(client_id="cid", access_token="tok")
    client.retry_base_delay = 0
    yield repo, client
    await client.aclose()
    await repo.close()


# ---------------------------------------------------------------------------------------------------- F3: any push error

async def three_titles(repo, sync, ops):
    """One title per simkl id 1, 2, 3, each with one queued row of the kind given (in that order)."""
    ids = []
    for n, op in zip((1, 2, 3), ops):
        sid = await repo.upsert_show(Show(title=f"T{n}", ids={"simkl": n}, media_type=MediaType.SHOW,
                                          status=ShowStatus.WATCHING))
        ids.append(sid)
        if op == "status":
            await sync.update_show_status(sid, ShowStatus.HOLD)
        elif op == "rate":
            await sync.update_show_rating(sid, 7)
        else:
            await sync.set_episodes_watched(sid, [(1, 1)], True)
    return ids


async def flush_with_one_bad_title(env, ops):
    repo, client = env
    sync = simkl_engine(client, repo)
    await three_titles(repo, sync, ops)
    sent = []
    respx.post(url__startswith=SIMKL_BASE_URL).mock(
        side_effect=lambda r: sent.append(r.url.path) or httpx.Response(201, json={}))
    original = sync.library.push
    pushes = []

    async def flaky(batch):
        pushes.append([o.ids["simkl"] for o in batch])
        if any(o.ids["simkl"] == 2 for o in batch):
            raise RuntimeError("unexpected")  # a KeyError building a body, an error the driver did not map
        return await original(batch)

    sync.library.push = flaky
    sent_count = await sync.flush_outbox()  # must not raise
    return repo, sync, sent_count, sent, pushes


@respx.mock
async def test_F3_R4_a_batch_that_raises_is_not_resent_now_and_goes_out_one_row_at_a_time_next_time(env):
    """The engine cannot know whether the request left before a non-SyncError was raised, so the rows of the batch
    all back off (no immediate resend) and are marked to be sent alone; the row that raises then backs off alone."""
    repo, sync, count, sent, pushes = await flush_with_one_bad_title(env, ["status"] * 3)
    assert pushes == [[1, 2, 3]] and count == 0 and sent == []  # one try, no resend by one
    rows = await repo.get_outbox()
    assert [(r["remote_id"], r["attempts"]) for r in rows] == [(1, 1), (2, 1), (3, 1)]
    assert all("RuntimeError" in r["last_error"] and r["payload"]["solo"] for r in rows)

    await repo.release_outbox_backoff()
    assert await sync.flush_outbox() == 2
    assert pushes[1:] == [[1], [2], [3]]  # each alone now
    assert len(sent) == 2
    assert [(r["remote_id"], r["attempts"]) for r in await repo.get_outbox()] == [(2, 2)]


@respx.mock
async def test_F3_a_row_whose_push_raises_backs_off_and_the_rows_after_it_go_out_one_request_each(env):
    repo, sync, count, sent, pushes = await flush_with_one_bad_title(env, ["watched", "rate", "status"])
    assert pushes == [[1], [2], [3]]  # three kinds of request: never batched together
    rows = await repo.get_outbox()
    assert [(r["remote_id"], r["attempts"]) for r in rows] == [(2, 1)]
    assert count == 2 and len(sent) == 2
    # it is held back like any failed row, and the next flush leaves it until it is due
    assert await sync.flush_outbox() == 0
    assert [r["attempts"] for r in await repo.get_outbox()] == [1]


# ---------------------------------------------------------------------------------------------------- F4: catalogue choice

class FakeTvmaze:
    """A TVmaze-like catalogue: resolve by TheTVDB id gives the whole series (TVmaze has one show per series, while
    SIMKL lists each anime season as its own title); episodes are numbered per series season."""

    key_provider = "tvmaze"

    async def ready(self):
        return True

    async def resolve(self, media_type, ids):
        if ids.get("tvdb") == 267440:
            return {**ids, "tvmaze": 919, "imdb": "tt2560140", "tvdb": 267440}, "anime"
        return None

    async def title(self, media_type, ids):
        return Title(media_type="anime", title="Attack on Titan", ids={"tvmaze": 919}, genres=["Anime"])

    async def episodes(self, media_type, ids):
        return [Episode(season=s, number=n, air_date="2020-01-01") for s in (1, 2, 3) for n in (1, 2)]

    async def related(self, media_type, ids):
        return []

    async def search(self, *a, **k):
        return []


@respx.mock
async def test_F4_a_titles_episode_list_comes_from_the_library_drivers_catalogue_when_it_has_its_id(env):
    """drivers: {library: simkl, catalog: [tvmaze, simkl]}. A SIMKL season title that also got the series' TVmaze id
    keeps SIMKL's numbering: its pushes use the numbering of the account they go to."""
    from penguwatch.api.discover import discover_add
    from penguwatch.api.schemas import DiscoverAddRequest

    repo, client = env
    repo.key_providers = tuple(identity_providers("simkl", ["tvmaze", "simkl"]))
    sync = simkl_engine(client, repo)
    fake = FakeTvmaze()
    sync.catalogs = [fake, sync.service.catalog]
    simkl_catalog = sync.service.catalog
    respx.get(url__regex=rf"{SIMKL_BASE_URL}/(tv|anime)/episodes/7001").respond(json=[
        {"title": "A", "season": 1, "episode": n, "type": "episode", "date": "2020-01-01"} for n in (1, 2, 3)])
    respx.get(url__startswith=SIMKL_BASE_URL).respond(json={})
    c = SimpleNamespace(background=set(), config=AppConfig())

    s2 = await discover_add(DiscoverAddRequest(title="Attack on Titan Season 2", ids={"simkl": 7001, "tvdb": 267440},
                                               media_type="anime"), repo, sync, c)
    assert s2.ids["tvmaze"] == 919 and s2.ids["simkl"] == 7001  # it has both
    assert sync.catalog_for(s2) is simkl_catalog  # the library driver's, though TVmaze is first in the chain

    # otherwise today's order: the first catalogue the title has an id for
    only_tvmaze = Show(title="x", ids={"tvmaze": 1}, media_type=MediaType.SHOW)
    only_simkl = Show(title="y", ids={"simkl": 1}, media_type=MediaType.SHOW)
    both = Show(title="z", ids={"simkl": 1, "tvmaze": 1}, media_type=MediaType.SHOW)
    neither = Show(title="w", ids={"tmdb": 1}, media_type=MediaType.SHOW)
    assert sync.catalog_for(only_tvmaze) is fake and sync.catalog_for(only_simkl) is simkl_catalog
    assert sync.catalog_for(neither) is None
    sync.catalogs = [fake]  # the library driver is not in the chain: the chain's order decides
    assert sync.catalog_for(both) is fake
    sync.catalogs = [fake, simkl_catalog]
    assert sync.catalog_for(both) is simkl_catalog

    # the episode list that the '+1' and season buttons use is the library's: numbered as its account numbers it
    await sync.set_season_watched(s2.id, 1, True)
    rows = [r for r in await repo.get_outbox() if r["op"] == "watched"]
    assert rows and rows[-1]["operation"].episodes == [(1, 1), (1, 2), (1, 3)]  # SIMKL's three, not TVmaze's two


# ---------------------------------------------------------------------------------------------------- F5: add before login

class Catalogue:
    """A catalogue that knows one title; keyed by the library driver's id, so the title gets a simkl id on adding."""

    key_provider = "simkl"

    async def ready(self):
        return True

    async def search(self, query, *a, **k):
        return [Title(media_type="show", title="Found", ids={"simkl": 7777}, year=2020)] if query == "Found" else []

    async def title(self, *a, **k):
        return None

    async def episodes(self, *a, **k):
        return None


@pytest.fixture
async def unconnected(tmp_path):
    """The simkl driver before the person has logged in: no token."""
    repo = DatabaseRepository(str(tmp_path / "early.db"), key_providers=("simkl",))
    await repo.initialize([ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99, aliases=["Apple TV+"])])
    await repo.set_state("sync_review_mode", "off")
    client = SimklClient(client_id="cid", access_token=None)
    client.retry_base_delay = 0
    sync = simkl_engine(client, repo)
    sync.catalogs = [Catalogue()]
    yield repo, client, sync
    await client.aclose()
    await repo.close()


async def add_found(repo, sync):
    from penguwatch.api.titles import _create_title
    return await _create_title(ShowCreateRequest(title="Found", year=2020), repo, sync, set())


@respx.mock
async def test_F5_a_title_added_before_the_login_is_queued_kept_by_the_first_full_pull_and_sent(unconnected):
    repo, client, sync = unconnected
    found = await add_found(repo, sync)
    assert found.ids == {"simkl": 7777}
    rows = await repo.get_outbox()
    assert [(r["op"], r["approved"], r["remote_id"]) for r in rows] == [("status", True, 7777)]
    assert rows[0]["payload"]["if_missing"] == "add"  # "add if missing": held until a full pull

    # not connected: the worker's calls skip, they do not fail, and nothing is sent
    assert await sync.flush_outbox() == 0
    assert (await sync.sync())["status"] == "skipped"
    assert not respx.calls
    assert [r["attempts"] for r in await repo.get_outbox()] == [0]

    # connect; SIMKL's listing does not have the title: the pull must keep it, and the add is released
    client.access_token = "tok"
    mock_reads(shows=[SEVERANCE], movies=[MOVIE])
    assert await sync.flush_outbox() == 0  # held until a full pull has shown what the account has
    result = await sync.sync(deep=False)  # no force_full: the held row makes it one
    assert result["status"] == "success" and result["mode"] == "full" and result["pushed"] == 0
    assert await repo.get_show_by_external_id("simkl", 7777) is not None  # not removed as "missing from the listing"
    rows = await repo.get_outbox()
    assert len(rows) == 1 and not rows[0]["payload"].get("if_missing")

    # the add goes out once the service answers
    sent = []
    respx.post(f"{SIMKL_BASE_URL}/sync/add-to-list").mock(
        side_effect=lambda r: sent.append(r.read()) or httpx.Response(201, json={}))
    assert await sync.flush_outbox() == 1
    assert len(sent) == 1 and b"7777" in sent[0]
    assert await repo.get_outbox() == []
    assert await repo.get_show_by_external_id("simkl", 7777) is not None


@respx.mock
async def test_F5_with_review_mode_on_the_early_add_waits_for_approval_in_the_review(unconnected):
    from penguwatch.api.sync_review import get_sync_review
    repo, client, sync = unconnected
    await repo.set_state("sync_review_mode", "on")
    await add_found(repo, sync)
    review = await get_sync_review(repo, sync)
    assert [(o["op"], o["approved"]) for o in review["outgoing"]] == [("status", False)]
    assert await sync.approve_outgoing() == 1
    assert await sync.flush_outbox() == 0  # approved, but still no login: it waits
    assert not respx.calls
    assert [r["approved"] for r in await repo.get_outbox()] == [True]


async def test_F5_a_library_without_an_account_still_queues_nothing_for_an_added_title(tmp_path):
    from tests.local_helpers import local_engine
    repo = DatabaseRepository(str(tmp_path / "local.db"), key_providers=tuple(identity_providers("local", ["simkl"])))
    await repo.initialize([])
    local = local_engine(repo, catalogs=[Catalogue()])
    found = await add_found(repo, local)
    assert "simkl" in found.ids and "local" in found.ids
    assert await repo.get_outbox() == []
    await repo.close()
