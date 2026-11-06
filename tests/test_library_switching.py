"""Switching the library driver (simkl <-> local).

- simkl to local: every title, episode, watch and rating stays; changes queued for simkl stay queued, undelivered; no
  request is made while local is active.
- edits made while local (watched, status, rating, delete) are kept for simkl as UNAPPROVED outbox rows, whatever the
  review-mode setting. Back on simkl a pull leaves those titles alone until the person approves the rows (they go out)
  or discards them (the account's version comes back).
- local back to simkl: the queued rows are delivered as usual; a pull's "missing from the listing" rule never removes
  a title that was added while local (one without a simkl id, or one born local that has one).
"""

import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from penguwatch.db.repository import DatabaseRepository
from penguwatch.drivers import identity_providers
from penguwatch.drivers.base import LOCAL_ID
from penguwatch.drivers.simkl.client import SIMKL_BASE_URL, SimklClient
from penguwatch.sync.active import LAST_ACCOUNT_KEY, build_drivers, remember_account
from penguwatch.models import MediaType, ServiceConfig, Show, ShowCreateRequest, ShowStatus
from tests.local_helpers import local_engine
from tests.simkl_helpers import simkl_engine
from tests.test_two_way_sync import MOVIE, SEVERANCE, mock_reads


@pytest.fixture
async def library(tmp_path):
    """A library as the simkl driver leaves it: three titles with simkl ids, one watched episode, one rating."""
    repo = DatabaseRepository(str(tmp_path / "switch.db"), key_providers=("simkl",))
    await repo.initialize([ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99, aliases=["Apple TV+"])])
    ids = {}
    for title, simkl, media, status, rating in (
            ("Alpha", 1001, MediaType.SHOW, ShowStatus.WATCHING, 8.0),
            ("Beta", 1002, MediaType.SHOW, ShowStatus.PLANTOWATCH, None),
            ("Gamma", 3001, MediaType.MOVIE, ShowStatus.COMPLETED, 7.0)):
        ids[title] = await repo.upsert_show(Show(title=title, ids={"simkl": simkl}, media_type=media, status=status,
                                                 user_rating=rating, total_episodes=3, aired_episodes=3))
    await repo.upsert_episode_list(ids["Alpha"], [
        {"season": 1, "number": n, "title": f"Ep {n}", "air_date": "2026-01-01"} for n in (1, 2, 3)])
    await repo.set_state("sync_review_mode", "off")
    client = SimklClient(client_id="cid", access_token="tok")
    client.retry_base_delay = 0
    yield repo, client, ids
    await client.aclose()
    await repo.close()


async def snapshot(repo):
    """Everything the user could lose: titles with status, rating and ids, and each episode's watched flag."""
    shows = sorted((s.title, s.status.value, s.user_rating, tuple(sorted(s.ids.items())), s.watched_episodes)
                   for s in await repo.get_shows())
    episodes = []
    for show in await repo.get_shows():
        episodes += [(show.title, e.season, e.number, e.is_watched) for e in await repo.get_episodes(show.id)]
    return shows, sorted(episodes)


@respx.mock
async def test_simkl_to_local_keeps_every_title_watch_and_rating_and_the_queued_rows(library):
    repo, client, ids = library
    simkl = simkl_engine(client, repo)
    await simkl.set_episodes_watched(ids["Alpha"], [(1, 1)], True)
    await simkl.update_show_status(ids["Beta"], ShowStatus.WATCHING)
    queued = await repo.get_outbox()
    assert [r["driver"] for r in queued] == ["simkl", "simkl"]
    before = await snapshot(repo)

    local = local_engine(repo)  # switching: nothing is sent while local is active
    assert await local.flush_outbox() == 0
    assert (await local.sync())["status"] == "success"
    assert not respx.calls  # no request at all (unmocked requests would raise)
    assert await snapshot(repo) == before
    after = await repo.get_outbox()
    assert [(r["id"], r["driver"], r["op"], r["approved"], r["attempts"], r["operation"].to_json()) for r in after] == \
           [(r["id"], r["driver"], r["op"], r["approved"], r["attempts"], r["operation"].to_json()) for r in queued]


@respx.mock
async def test_changes_made_while_local_with_no_account_ever_write_no_outbox_row_and_contact_nothing(library):
    repo, client, ids = library
    local = local_engine(repo)
    await repo.set_state("sync_review_mode", "on")  # stored, but meaningless without an account
    # a title added while local has a local id: the one id a careless "has an id, so push it" rule would accept
    born = await repo.upsert_show(Show(title="Born local", ids={LOCAL_ID: "abc"}, media_type=MediaType.SHOW,
                                       status=ShowStatus.WATCHING))
    await local.update_show_status(born, ShowStatus.HOLD)
    await local.set_episodes_watched(ids["Alpha"], [(1, 1), (1, 2)], True)
    await local.update_show_status(ids["Beta"], ShowStatus.DROPPED)
    await local.remove_from_library(await repo.get_show_by_id(ids["Gamma"]))
    await local.update_show_rating(ids["Alpha"], 9)
    await local.queue_list_move(await repo.get_show_by_id(ids["Gamma"]), ShowStatus.DROPPED)
    assert await repo.get_outbox() == []  # no last account was ever recorded here: nothing to keep the edits for
    assert not respx.calls
    alpha = await repo.get_episodes(ids["Alpha"])
    assert [(e.number, e.is_watched) for e in alpha] == [(1, True), (2, True), (3, False)]  # the watch was recorded
    assert (await repo.get_show_by_id(ids["Beta"])).status == ShowStatus.DROPPED
    assert (await repo.get_show_by_id(ids["Alpha"])).user_rating == 9
    assert await local.review_mode() is False  # review mode is meaningless without an account, whatever is stored


@respx.mock
async def test_local_back_to_simkl_delivers_the_rows_queued_before(library):
    repo, client, ids = library
    simkl = simkl_engine(client, repo)
    await simkl.set_episodes_watched(ids["Alpha"], [(1, 1)], True)
    await simkl.update_show_status(ids["Beta"], ShowStatus.WATCHING)
    local = local_engine(repo)
    await local.set_episodes_watched(ids["Alpha"], [(1, 2)], True)  # made while local: no account recorded, not queued
    assert len(await repo.get_outbox()) == 2

    bodies = []
    respx.post(url__startswith=SIMKL_BASE_URL).mock(side_effect=lambda r: bodies.append(r.url.path) or httpx.Response(
        201, json={"not_found": {"shows": [], "movies": []}}))
    assert await simkl_engine(client, repo).flush_outbox() == 2
    assert sorted(bodies) == ["/sync/add-to-list", "/sync/history"]
    assert await repo.get_outbox() == []


def mock_listing(shows, movies):
    mock_reads(shows=shows, movies=movies)


async def born_local_titles(repo):
    """What a person adds while local: one title found through a catalogue (a simkl id), one added by hand (none)."""
    class Catalogue:
        key_provider = "simkl"

        async def ready(self):
            return True

        async def search(self, query, *a, **k):
            from penguwatch.drivers.base import Title
            return [Title(media_type="show", title="Found", ids={"simkl": 7777}, year=2020)] if query == "Found" else []

        async def title(self, *a, **k):
            return None

        async def episodes(self, *a, **k):
            return None

    from penguwatch.api.titles import _create_title
    local = local_engine(repo, catalogs=[Catalogue()])
    found = await _create_title(ShowCreateRequest(title="Found", year=2020), repo, local, set())
    by_hand = await _create_title(ShowCreateRequest(title="By hand"), repo, local, set())
    return local, found, by_hand


@respx.mock
async def test_titles_added_while_local_are_not_removed_when_simkl_pulls_its_listing(library):
    repo, client, ids = library
    repo.key_providers = tuple(identity_providers("local", ["simkl"]))
    _, found, by_hand = await born_local_titles(repo)
    assert found.ids["simkl"] == 7777 and LOCAL_ID in found.ids and LOCAL_ID in by_hand.ids
    assert "simkl" not in by_hand.ids
    await repo.upsert_show(Show(title="Untracked elsewhere", ids={"tmdb": 5}, media_type=MediaType.SHOW))

    # SIMKL's account lists only Severance and Dune: the library's Alpha, Beta and Gamma are gone from it
    mock_listing([SEVERANCE], [MOVIE])
    result = await simkl_engine(client, repo).sync(deep=False)
    titles = {s.title for s in await repo.get_shows()}
    assert result["status"] == "success"
    assert {"Found", "By hand", "Untracked elsewhere"} <= titles  # added while local: kept
    assert not ({"Alpha", "Beta", "Gamma"} & titles)  # control: the rule is live, and removes what SIMKL dropped


@respx.mock
async def test_a_title_without_a_simkl_id_is_never_a_removal_candidate(library):
    repo, client, ids = library
    plain = await repo.upsert_show(Show(title="No id", ids={"tmdb": 5}, media_type=MediaType.SHOW))
    mock_listing([SEVERANCE], [MOVIE])
    simkl = simkl_engine(client, repo)
    assert list((await repo.get_external_ids("simkl")).values()).count(plain) == 0
    await simkl.sync(deep=False)
    assert await repo.get_show_by_id(plain) is not None


@respx.mock
async def test_adding_the_same_title_twice_while_local_finds_it_through_its_catalogue_id(library):
    repo, client, ids = library
    repo.key_providers = tuple(identity_providers("local", ["simkl"]))  # as the storage component sets it
    local, first, _ = await born_local_titles(repo)
    from penguwatch.api.titles import _create_title
    again = await _create_title(ShowCreateRequest(title="Found", year=2020), repo, local, set())
    assert again.id == first.id
    assert [s.title for s in await repo.get_shows()].count("Found") == 1


async def test_the_engine_never_delivers_for_a_library_without_an_account(library):
    """Even a row somehow queued under the local driver's name is left alone: nothing is pushed, nothing deleted."""
    repo, client, ids = library
    from penguwatch.drivers.base import OP_STATUS, SyncOp
    local = local_engine(repo)
    row = await repo.enqueue_outbox("local", SyncOp(kind=OP_STATUS, media_type="show", ids={"local": "x"},
                                                    status="hold"), ids["Alpha"], "x")
    pushed = []
    local.library.push = lambda ops: pushed.append(ops)
    assert await local.flush_outbox() == 0
    assert pushed == []  # push is never called
    assert [(r["id"], r["attempts"]) for r in await repo.get_outbox()] == [(row, 0)]  # untouched: no failed attempt


# ---------------------------------------------------------------------------------------------------------------------
# Edits made while local are kept for the account: unapproved rows that protect the title from the next pull

@pytest.fixture
async def imported(tmp_path):
    """A library imported from simkl (Severance, Dune), review mode off, episode list known."""
    repo = DatabaseRepository(str(tmp_path / "kept.db"), key_providers=("simkl",))
    await repo.initialize([ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99, aliases=["Apple TV+"])])
    await repo.set_state("sync_review_mode", "off")
    client = SimklClient(client_id="cid", access_token="tok")
    client.retry_base_delay = 0
    with respx.mock:
        mock_reads(shows=[SEVERANCE], movies=[MOVIE])
        assert (await simkl_engine(client, repo).sync(deep=False))["status"] == "success"
    sev = await repo.get_show_by_external_id("simkl", 1400298)
    await repo.upsert_episode_list(sev.id, [{"season": 1, "number": n, "air_date": "2026-01-01"} for n in (1, 2, 3)])
    await repo.update_show_fields(sev.id, {"episodes_synced_at": "2099-01-01T00:00:00+00:00"})
    yield repo, client, sev.id
    await client.aclose()
    await repo.close()


async def go_local(repo):
    """Switch to the local library: the simkl driver ran before (the drivers component records it when it starts)."""
    await remember_account(repo, "simkl")
    return local_engine(repo)


async def watched_set(repo, show_id):
    return {(e.season, e.number) for e in await repo.get_episodes(show_id) if e.is_watched}


@respx.mock
async def test_the_drivers_component_records_the_account_driver_and_local_leaves_it(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "rec.db"))
    await repo.initialize([])

    def config(library):
        roles = SimpleNamespace(library=library, catalog=["simkl"], schedule=[], discovery=[])
        return SimpleNamespace(drivers=roles, driver_options=lambda name: DRIVER_OPTIONS[name])

    from penguwatch.drivers.simkl.options import SimklOptions
    DRIVER_OPTIONS = {"simkl": SimklOptions(), "local": None}
    assert await repo.get_state(LAST_ACCOUNT_KEY) is None
    first = await build_drivers(config("local"), repo)
    assert await repo.get_state(LAST_ACCOUNT_KEY) is None  # never had an account
    await first.aclose()
    second = await build_drivers(config("simkl"), repo)
    assert await repo.get_state(LAST_ACCOUNT_KEY) == "simkl"
    await second.aclose()
    third = await build_drivers(config("local"), repo)
    assert await repo.get_state(LAST_ACCOUNT_KEY) == "simkl"  # local does not erase it
    await third.aclose()
    await repo.close()


@respx.mock
async def test_F1_watched_status_and_rating_made_while_local_survive_the_first_pull_after_switching_back(imported):
    repo, client, sev_id = imported
    local = await go_local(repo)
    await local.set_episodes_watched(sev_id, [(1, 1), (1, 2), (1, 3)], True)
    await local.update_show_status(sev_id, ShowStatus.COMPLETED)
    await local.update_show_rating(sev_id, 10)

    rows = await repo.get_outbox()
    assert [(r["driver"], r["op"], r["approved"], r["remote_id"]) for r in rows] == [
        ("simkl", "watched", False, 1400298), ("simkl", "status", False, 1400298), ("simkl", "rate", False, 1400298)]
    assert not respx.calls  # nothing sent while local
    from penguwatch.api.drivers import _inactive_outbox
    assert _inactive_outbox(rows, "local") == {"simkl": 3}  # counted as waiting for an inactive driver

    mock_reads(shows=[SEVERANCE], movies=[MOVIE])  # SIMKL still has its own state
    simkl = simkl_engine(client, repo)
    assert await simkl.review_mode() is False
    result = await simkl.sync(force_full=True, deep=False)
    assert result["status"] == "success" and result["pushed"] == 0  # unapproved rows are not sent, review mode off
    assert result["awaiting_approval"] == 3
    after = await repo.get_show_by_id(sev_id)
    assert (after.status, after.user_rating) == (ShowStatus.COMPLETED, 10)
    assert await watched_set(repo, sev_id) == {(1, 1), (1, 2), (1, 3)}
    assert len(await repo.get_outbox()) == 3


@respx.mock
async def test_F1b_a_title_deleted_while_local_stays_deleted_and_is_not_staged_after_switching_back(imported):
    from penguwatch.api.shows import delete_show
    repo, client, sev_id = imported
    local = await go_local(repo)
    assert (await delete_show(sev_id, True, repo, local))["removed_from_library"] is True
    assert await repo.get_show_by_id(sev_id) is None
    rows = await repo.get_outbox()
    assert [(r["driver"], r["op"], r["approved"], r["remote_id"]) for r in rows] == [("simkl", "remove", False, 1400298)]
    assert "Severance" in rows[0]["summary"]  # the title is gone, the row still says which one

    for review in ("off", "on"):
        await repo.set_state("sync_review_mode", review)
        mock_reads(shows=[SEVERANCE], movies=[MOVIE])
        result = await simkl_engine(client, repo).sync(force_full=True, deep=False)
        assert result["status"] == "success"
        assert await repo.get_show_by_external_id("simkl", 1400298) is None  # not re-imported
        assert not result.get("staged")  # and not staged for re-import either
        assert await repo.get_state("incoming_staged") is None
        assert len(await repo.get_outbox()) == 1
    assert await repo.get_show_by_external_id("simkl", 2222) is not None  # control: the pull did run


async def test_the_remove_row_survives_the_title_being_deleted_after_it_was_queued(imported):
    repo, client, sev_id = imported
    local = await go_local(repo)
    await local.update_show_rating(sev_id, 10)
    await local.remove_from_library(await repo.get_show_by_id(sev_id))
    assert await repo.delete_show(sev_id)
    # the other rows of the deleted title are dropped with it, the removal is not
    assert [(r["op"], r["approved"]) for r in await repo.get_outbox()] == [("remove", False)]


async def test_F1_unapproved_rows_are_listed_with_review_mode_off_and_never_sent_until_approved(imported):
    from penguwatch.api.sync_review import get_sync_review
    repo, client, sev_id = imported
    local = await go_local(repo)
    await local.update_show_rating(sev_id, 10)
    simkl = simkl_engine(client, repo)
    assert await simkl.review_mode() is False
    review = await get_sync_review(repo, simkl)
    assert review["review_mode"] is False
    assert [(o["op"], o["approved"]) for o in review["outgoing"]] == [("rate", False)]
    assert review["outgoing"][0]["summary"] == "Rate 10/10"
    assert await simkl.flush_outbox() == 0  # approved=0 is never delivered, with review mode off too
    assert [r["attempts"] for r in await repo.get_outbox()] == [0]


@respx.mock
async def test_F1_approving_a_kept_row_sends_it(imported):
    repo, client, sev_id = imported
    local = await go_local(repo)
    await local.update_show_rating(sev_id, 10)
    await local.update_show_status(sev_id, ShowStatus.COMPLETED)
    sent = []
    respx.post(url__startswith=SIMKL_BASE_URL).mock(side_effect=lambda r: sent.append(r.url.path) or httpx.Response(
        201, json={"not_found": {"shows": [], "movies": []}}))
    simkl = simkl_engine(client, repo)
    rate_row = next(r["id"] for r in await repo.get_outbox() if r["op"] == "rate")
    assert await simkl.approve_outgoing([rate_row]) == 1
    assert await simkl.flush_outbox() == 1  # only the approved one
    assert sent == ["/sync/ratings"]
    assert [(r["op"], r["approved"]) for r in await repo.get_outbox()] == [("status", False)]
    assert (await repo.get_show_by_id(sev_id)).user_rating == 10


@respx.mock
async def test_F1_discarding_kept_rows_takes_the_accounts_version_and_unlocks_the_title(imported):
    repo, client, sev_id = imported
    local = await go_local(repo)
    await local.set_episodes_watched(sev_id, [(1, 3)], True)
    await local.update_show_status(sev_id, ShowStatus.COMPLETED)
    await local.update_show_rating(sev_id, 10)
    simkl = simkl_engine(client, repo)
    mock_reads(shows=[SEVERANCE], movies=[MOVIE])
    out = await simkl.discard_outgoing([r["id"] for r in await repo.get_outbox()])
    assert out["discarded"] == 3 and out["restored"] == 1
    assert await repo.get_outbox() == []
    after = await repo.get_show_by_id(sev_id)
    assert (after.status, after.user_rating) == (ShowStatus.WATCHING, 9)  # SIMKL's
    assert await watched_set(repo, sev_id) == {(1, 1), (1, 2)}
    assert not [c for c in respx.calls if c.request.method == "POST"]  # nothing was sent


@respx.mock
async def test_F1_discarding_the_removal_brings_the_title_back(imported):
    from penguwatch.api.shows import delete_show
    repo, client, sev_id = imported
    local = await go_local(repo)
    await delete_show(sev_id, True, repo, local)
    simkl = simkl_engine(client, repo)
    mock_reads(shows=[SEVERANCE], movies=[MOVIE])
    out = await simkl.discard_outgoing([r["id"] for r in await repo.get_outbox()])
    assert out["restored"] == 1
    back = await repo.get_show_by_external_id("simkl", 1400298)
    assert back is not None and back.title == "Severance"


async def test_F1_nothing_is_kept_for_a_title_the_account_does_not_know_or_that_was_born_local(imported):
    repo, client, sev_id = imported
    local = await go_local(repo)
    plain = await repo.upsert_show(Show(title="No simkl id", ids={"tmdb": 5}, media_type=MediaType.SHOW))
    # added while local through a catalogue: it has a simkl id AND the local id (it was never on the account)
    born = await repo.upsert_show(Show(title="Found", ids={"simkl": 7777, LOCAL_ID: "abc"}, media_type=MediaType.SHOW,
                                       status=ShowStatus.WATCHING))
    for show_id in (plain, born):
        await local.update_show_status(show_id, ShowStatus.HOLD)
        await local.update_show_rating(show_id, 7)
        await local.set_episodes_watched(show_id, [(1, 1)], True)
        await local.remove_from_library(await repo.get_show_by_id(show_id))
    assert await repo.get_outbox() == []


async def test_nothing_is_kept_when_no_account_driver_ever_ran(imported):
    repo, client, sev_id = imported
    local = local_engine(repo)  # no record: the person started on local
    await local.update_show_rating(sev_id, 10)
    assert await repo.get_outbox() == []


@respx.mock
async def test_plus_one_while_local_is_kept_for_the_account_even_when_the_episode_list_cannot_be_refreshed(imported):
    """'+1' on a title from the account, while local and with a stale episode list that no catalogue can refresh
    (offline, or no catalogue for it): the fallback still queues the episode for the account, so switching back cannot
    undo the count."""
    repo, _, sev_id = imported
    await repo.update_show_fields(sev_id, {"episodes_synced_at": "2000-01-01T00:00:00+00:00", "next_to_watch": "S01E01"})
    local = await go_local(repo)
    local.catalogs = []  # nothing can refresh the episode list
    assert not await local.ensure_episode_list(await repo.get_show_by_id(sev_id))
    await local.mark_next_episode(sev_id)
    rows = await repo.get_outbox()
    assert [(r["driver"], r["op"], r["approved"]) for r in rows] == [("simkl", "watched", False)]
    assert (1, 1) in await watched_set(repo, sev_id)
    assert not respx.calls
