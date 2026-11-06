"""Round 2 of the driver layer's data-safety fixes.

R1  a newer change replaces the older unapproved rows it contradicts (approved rows are never touched)
R2  an add made before the first login is "add if missing": held until a full pull, the account wins
R3  rows of a driver that is not the connected library driver cannot be discarded (409)
"""

import json

import httpx
import pytest
import respx
from fastapi import HTTPException

from penguwatch.api.schemas import OutgoingDiscardRequest
from penguwatch.api.sync_review import discard_outgoing, get_sync_review
from penguwatch.db.repository import DatabaseRepository
from penguwatch.drivers.base import (
    OP_RATE, OP_REMOVE, OP_STATUS, OP_UNRATE, OP_UNWATCHED, OP_WATCHED, SyncOp,
)
from penguwatch.drivers.base import Title
from penguwatch.drivers.simkl.client import SIMKL_BASE_URL, SimklClient
from penguwatch.models import MediaType, ServiceConfig, Show, ShowCreateRequest, ShowStatus
from penguwatch.sync.active import remember_account
from tests.local_helpers import local_engine
from tests.simkl_helpers import simkl_engine
from tests.test_two_way_sync import ACTIVITIES_1, SEVERANCE, mock_reads


@pytest.fixture
async def env(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "round2.db"), key_providers=("simkl",))
    await repo.initialize([ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99, aliases=["Apple TV+"])])
    await repo.set_state("sync_review_mode", "off")
    client = SimklClient(client_id="cid", access_token="tok")
    client.retry_base_delay = 0
    yield repo, client
    await client.aclose()
    await repo.close()


def capture_posts():
    sent = []

    def answer(request):
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(201, json={"not_found": {"shows": [], "movies": []}})

    respx.post(url__startswith=SIMKL_BASE_URL).mock(side_effect=answer)
    return sent


def op(kind, episodes=(), media="show", **kw):
    return SyncOp(kind=kind, media_type=media, ids={"simkl": 1}, episodes=list(episodes), **kw)


async def add(repo, o, approved=False, sid=1):
    return await repo.enqueue_outbox("simkl", o, None, sid, approved=approved)


async def rows(repo):
    return [(r["op"], r["approved"], r["operation"].episodes) for r in await repo.get_outbox()]


# ------------------------------------------------------------------------------------------------------------- R1

@respx.mock
async def test_R1_a_kept_row_approved_later_cannot_overwrite_a_newer_change_already_sent(env):
    repo, client = env
    mock_reads(shows=[SEVERANCE], movies=[])
    await simkl_engine(client, repo).sync(deep=False)
    await remember_account(repo, "simkl")
    sev = await repo.get_show_by_external_id("simkl", 1400298)
    await local_engine(repo).update_show_rating(sev.id, None)  # while local: kept for SIMKL, unapproved
    assert [(r["op"], r["approved"]) for r in await repo.get_outbox()] == [("unrate", False)]

    simkl = simkl_engine(client, repo)  # switched back, review mode off
    sent = capture_posts()
    await simkl.update_show_rating(sev.id, 8)
    assert [(r["op"], r["approved"]) for r in await repo.get_outbox()] == [("rate", True)]  # the old unrate is gone
    assert await simkl.flush_outbox() == 1
    await simkl.approve_outgoing()  # "Approve everything"
    assert await simkl.flush_outbox() == 0
    assert [p for p, _ in sent] == ["/sync/ratings"]  # SIMKL keeps the 8


async def test_R1_status_replaces_older_unapproved_status_rows_only(env):
    repo, _ = env
    await add(repo, op(OP_STATUS, status="hold"))
    await add(repo, op(OP_RATE, rating=5))
    await add(repo, op(OP_STATUS, status="completed"), approved=True)  # approved: never touched
    await add(repo, op(OP_STATUS, status="dropped"))
    await add(repo, op(OP_STATUS, status="watching"), sid=2)  # another title
    assert [(o, a) for o, a, _ in await rows(repo)] == [("rate", False), ("status", True), ("status", False),
                                                       ("status", False)]
    statuses = [r["operation"].status for r in await repo.get_outbox() if r["op"] == "status"]
    assert statuses == ["completed", "dropped", "watching"]


async def test_R1_rate_and_unrate_replace_each_other(env):
    repo, _ = env
    await add(repo, op(OP_RATE, rating=5))
    await add(repo, op(OP_UNRATE))
    assert [o for o, _, _ in await rows(repo)] == ["unrate"]
    await add(repo, op(OP_RATE, rating=9))
    assert [(r["op"], r["operation"].rating) for r in await repo.get_outbox()] == [("rate", 9)]
    await add(repo, op(OP_STATUS, status="hold"))
    await add(repo, op(OP_RATE, rating=3), approved=True)
    assert [o for o, _, _ in await rows(repo)] == ["status", "rate"]  # the older rate is gone, the status stays


async def test_R1_remove_replaces_every_older_unapproved_row_of_the_title_and_no_approved_one(env):
    repo, _ = env
    await add(repo, op(OP_STATUS, status="hold"))
    await add(repo, op(OP_WATCHED, [(1, 1)]))
    await add(repo, op(OP_RATE, rating=4), approved=True)
    await add(repo, op(OP_RATE, rating=4), sid=2)
    await add(repo, op(OP_REMOVE))
    assert [(o, a) for o, a, _ in await rows(repo)] == [("rate", True), ("rate", False), ("remove", False)]


async def test_R1_watched_and_unwatched_take_their_episodes_out_of_older_unapproved_rows(env):
    repo, _ = env
    await add(repo, op(OP_WATCHED, [(1, 1), (1, 2), (1, 3)]))
    await add(repo, op(OP_WATCHED, [(2, 1)], watched_at="2026-01-01T00:00:00Z"), approved=True)
    await add(repo, op(OP_UNWATCHED, [(1, 2)]))
    assert await rows(repo) == [("watched", False, [(1, 1), (1, 3)]), ("watched", True, [(2, 1)]),
                                ("unwatched", False, [(1, 2)])]
    await add(repo, op(OP_WATCHED, [(1, 1), (1, 2), (1, 3)]))  # covers everything older: those rows are deleted
    assert await rows(repo) == [("watched", True, [(2, 1)]), ("watched", False, [(1, 1), (1, 2), (1, 3)])]


async def test_R1_a_movie_change_replaces_older_unapproved_watch_rows(env):
    repo, _ = env
    await add(repo, op(OP_WATCHED, media="movie", watched_at="2026-01-01T00:00:00Z"))
    await add(repo, op(OP_UNWATCHED, media="movie"))
    assert [o for o, _, _ in await rows(repo)] == ["unwatched"]


NATIVE = {"to": "hold", "shows": [{"ids": {"simkl": 1}, "to": "hold"}]}


async def test_Q6a_native_rows_are_replaced_whole_where_a_whole_row_rule_applies(env):
    repo, _ = env
    await add(repo, SyncOp(kind=OP_STATUS, media_type="show", ids={"simkl": 1}, native=NATIVE))
    await add(repo, SyncOp(kind=OP_RATE, media_type="show", ids={"simkl": 1}, native={"shows": []}))
    await add(repo, op(OP_STATUS, status="completed"))
    assert [o for o, _, _ in await rows(repo)] == ["rate", "status"]  # the native status is gone
    await add(repo, op(OP_UNRATE))
    assert [o for o, _, _ in await rows(repo)] == ["status", "unrate"]  # and the native rate
    await add(repo, SyncOp(kind=OP_WATCHED, media_type="show", ids={"simkl": 1}, native={"shows": []}))
    await add(repo, op(OP_REMOVE))
    assert [o for o, _, _ in await rows(repo)] == ["remove"]  # remove deletes everything, native included


async def test_Q6a_a_native_watch_row_is_left_and_the_newer_watch_row_waits_for_the_person(env):
    repo, _ = env
    native = await add(repo, SyncOp(kind=OP_WATCHED, media_type="show", ids={"simkl": 1}, native={"shows": []}))
    await add(repo, op(OP_UNWATCHED, [(1, 1)]), approved=True)  # would be sent first and invert the order
    assert [(r["id"], r["op"], r["approved"]) for r in await repo.get_outbox()][0] == (native, "watched", False)
    assert [(r["op"], r["approved"]) for r in await repo.get_outbox()] == [("watched", False), ("unwatched", False)]


async def test_Q6b_a_newer_status_deletes_an_older_unapproved_removal(env):
    repo, _ = env
    await add(repo, op(OP_REMOVE))
    await add(repo, op(OP_REMOVE), sid=2)
    await add(repo, op(OP_STATUS, status="plantowatch"))  # the person added the title again
    assert [(r["remote_id"], r["op"]) for r in await repo.get_outbox()] == [(2, "remove"), (1, "status")]
    await add(repo, op(OP_REMOVE), approved=True, sid=3)
    await add(repo, op(OP_STATUS, status="plantowatch"), sid=3)  # an approved removal stays: it goes out first
    assert [(r["remote_id"], r["op"]) for r in await repo.get_outbox() if r["remote_id"] == 3] == [(3, "remove"), (3, "status")]


async def test_Q5_a_trimmed_watch_row_describes_what_it_now_sends(env):
    repo, _ = env
    await add(repo, op(OP_WATCHED, [(1, 1), (1, 2), (2, 1)]))
    await repo.enqueue_outbox("simkl", op(OP_UNWATCHED, [(1, 2)]), None, 1, summary="Mark S01E02 unwatched", approved=False)
    first = (await repo.get_outbox())[0]
    assert first["operation"].episodes == [(1, 1), (2, 1)]
    assert "S01E02" not in first["summary"] and "S01E01" in first["summary"] and "S02E01" in first["summary"]


@respx.mock
async def test_R1_approving_several_rows_at_once_sends_each_titles_rows_in_id_order(env):
    repo, client = env
    simkl = simkl_engine(client, repo)
    await add(repo, op(OP_STATUS, status="hold"))
    await add(repo, op(OP_RATE, rating=6))
    await add(repo, op(OP_WATCHED, [(1, 1)], watched_at="2026-01-01T00:00:00Z"))
    await add(repo, op(OP_STATUS, status="hold"), sid=2)
    sent = capture_posts()
    await simkl.approve_outgoing()
    assert await simkl.flush_outbox() == 4
    order = [p for p, _ in sent]
    assert order.index("/sync/add-to-list") < order.index("/sync/ratings") < order.index("/sync/history")


# ------------------------------------------------------------------------------------------------------------- R2

class Catalogue:
    """Finds the titles it is given by name: "T<n>" has the SIMKL id n; "Severance" has 1400298."""

    key_provider = "simkl"

    async def ready(self):
        return True

    async def search(self, query, *a, **k):
        n = 1400298 if query == "Severance" else int(query[1:])
        return [Title(media_type="show", title=query, ids={"simkl": n}, year=2022)]

    async def title(self, *a, **k):
        return None

    async def episodes(self, *a, **k):
        return None


async def early_env(repo, review):
    await repo.set_state("sync_review_mode", review)
    client = SimklClient(client_id="cid", access_token=None)
    client.retry_base_delay = 0
    sync = simkl_engine(client, repo)
    sync.catalogs = [Catalogue()]
    return client, sync


async def add_early(repo, sync, name):
    from penguwatch.api.titles import _create_title
    return await _create_title(ShowCreateRequest(title=name, year=2022), repo, sync, set())


@pytest.mark.parametrize("review", ["off", "on"])
@respx.mock
async def test_R2_the_first_login_does_not_move_the_accounts_title_back_and_its_state_comes_in(env, review):
    repo, _ = env
    client, sync = await early_env(repo, review)
    created = await add_early(repo, sync, "Severance")
    assert created.status == ShowStatus.PLANTOWATCH
    client.access_token = "tok"  # the first login; the account has Severance: watching, rating 9, S1E1-2 seen
    mock_reads(shows=[SEVERANCE], movies=[])
    sent = capture_posts()
    assert await sync.flush_outbox() == 0  # held: not delivered before the full pull
    result = await sync.sync(deep=False)
    assert result["status"] == "success" and result["mode"] == "full"
    assert sent == []  # nothing was sent to the account
    assert await repo.get_outbox() == []  # the add was dropped: the account has the title
    if review == "on":  # changes from the account wait for the person, as always
        assert result["staged"] == 1
        assert (await repo.get_show_by_id(created.id)).status == ShowStatus.PLANTOWATCH
        await sync.resolve_incoming(None, True)
    show = await repo.get_show_by_id(created.id)
    assert (show.status, show.user_rating) == (ShowStatus.WATCHING, 9)
    assert {(e.season, e.number) for e in await repo.get_episodes(created.id) if e.is_watched} == {(1, 1), (1, 2)}
    await client.aclose()


@pytest.mark.parametrize("review", ["off", "on"])
@respx.mock
async def test_R2_an_add_for_a_title_the_account_lacks_is_kept_and_goes_out_after_the_full_pull(env, review):
    repo, _ = env
    client, sync = await early_env(repo, review)
    created = await add_early(repo, sync, "T555")
    client.access_token = "tok"
    mock_reads(shows=[SEVERANCE], movies=[])
    sent = capture_posts()
    result = await sync.sync(deep=False)
    assert result["status"] == "success" and sent == []  # held through the pull
    assert await repo.get_show_by_id(created.id) is not None  # not removed as "missing"
    [row] = await repo.get_outbox()
    assert not row["payload"].get("if_missing") and row["approved"] == (review == "off")  # a normal add now
    if review == "on":
        await sync.approve_outgoing()
    assert await sync.flush_outbox() == 1
    assert sent[0][0] == "/sync/add-to-list" and sent[0][1]["shows"][0]["ids"]["simkl"] == 555


@respx.mock
async def test_R2_a_long_account_free_period_only_the_missing_titles_are_sent(env):
    repo, _ = env
    client, sync = await early_env(repo, "off")
    ids = {}
    for n in range(101, 107):
        ids[n] = (await add_early(repo, sync, f"T{n}")).id
    await sync.update_show_status(ids[102], ShowStatus.HOLD)  # a later edit while disconnected: held with the add (kept for the person if the account has the title)
    client.access_token = "tok"
    on_account = [dict(SEVERANCE, status="completed", user_rating=7,
                       show={"title": f"T{n}", "year": 2022, "ids": {"simkl": n}}) for n in (101, 102, 103)]
    mock_reads(shows=on_account, movies=[])
    sent = capture_posts()
    assert (await sync.sync(deep=False))["status"] == "success"
    assert sent == []
    assert await sync.flush_outbox() == 3
    assert sorted(i["ids"]["simkl"] for p, b in sent for i in b["shows"]) == [104, 105, 106]
    for n in (101, 102, 103):  # the account's state, not the early adds'
        show = await repo.get_show_by_id(ids[n])
        assert (show.status, show.user_rating) == (ShowStatus.COMPLETED, 7)
    for n in (104, 105, 106):
        assert (await repo.get_show_by_id(ids[n])).status == ShowStatus.PLANTOWATCH
    # the edit made before the login (T102's status) waits for the person; nothing else is left
    [edit] = await repo.get_outbox()
    assert (edit["remote_id"], edit["op"], edit["approved"], edit["payload"].get("if_missing")) == (102, "status", False, None)
    await client.aclose()


@respx.mock
async def test_R2_a_delete_while_disconnected_stays_a_normal_unheld_removal(env):
    from penguwatch.api.shows import delete_show
    repo, _ = env
    client, sync = await early_env(repo, "off")
    created = await add_early(repo, sync, "T777")
    assert (await delete_show(created.id, True, repo, sync))["removed_from_library"] is True
    [row] = await repo.get_outbox()  # the removal replaced the (unapproved or not) add: only it is left
    assert row["op"] == "remove" and not row["payload"].get("if_missing")
    await client.aclose()


# ------------------------------------------------------------------------------------------------------------- R3

@respx.mock
async def test_R3_rows_of_a_driver_that_is_not_the_library_cannot_be_discarded(env):
    repo, client = env
    mock_reads(shows=[SEVERANCE], movies=[])
    await simkl_engine(client, repo).sync(deep=False)
    await remember_account(repo, "simkl")
    sev = await repo.get_show_by_external_id("simkl", 1400298)
    local = local_engine(repo)
    await local.update_show_status(sev.id, ShowStatus.DROPPED)
    row_ids = [r["id"] for r in await repo.get_outbox()]

    review = await get_sync_review(repo, local)
    assert review["outgoing"][0]["discard_blocked"] == "Switch the library back to SIMKL and connect to decide on these."
    with pytest.raises(HTTPException) as refused:
        await discard_outgoing(OutgoingDiscardRequest(ids=row_ids), local)
    assert refused.value.status_code == 409 and "Switch the library back to SIMKL" in refused.value.detail
    assert (await repo.get_show_by_id(sev.id)).status == ShowStatus.DROPPED
    assert [r["id"] for r in await repo.get_outbox()] == row_ids  # nothing was deleted
    assert await local.approve_outgoing(row_ids) == 1  # approving is still allowed: the row waits


@respx.mock
async def test_R3_rows_cannot_be_discarded_while_the_library_driver_is_not_connected(env):
    repo, client = env
    mock_reads(shows=[SEVERANCE], movies=[])
    await simkl_engine(client, repo).sync(deep=False)
    sev = await repo.get_show_by_external_id("simkl", 1400298)
    await repo.set_state("sync_review_mode", "on")
    simkl = simkl_engine(client, repo)
    await simkl.update_show_status(sev.id, ShowStatus.DROPPED)
    client.access_token = None  # logged out
    [row] = await repo.get_outbox()
    with pytest.raises(HTTPException) as refused:
        await discard_outgoing(OutgoingDiscardRequest(ids=[row["id"]]), simkl)
    assert refused.value.status_code == 409 and refused.value.detail == "Connect SIMKL to decide on these."
    assert len(await repo.get_outbox()) == 1
    client.access_token = "tok"  # connected again: it can be decided
    out = await simkl.discard_outgoing([row["id"]])
    assert out["discarded"] == 1


@respx.mock
async def test_R2_held_adds_make_the_next_pull_a_full_one_even_when_the_account_has_not_changed(env):
    repo, client = env
    mock_reads(shows=[SEVERANCE], movies=[])
    sync = simkl_engine(client, repo)
    sync.catalogs = [Catalogue()]
    assert (await sync.sync(deep=False))["mode"] == "full"
    assert (await sync.sync(deep=False))["mode"] == "delta"  # nothing changed: only a delta
    client.access_token = None  # logged out for a while
    created = await add_early(repo, sync, "T555")
    client.access_token = "tok"
    sent = capture_posts()
    result = await sync.sync(deep=False)
    assert result["mode"] == "full"  # the held add needs a listing
    assert await sync.flush_outbox() == 1 and sent[0][1]["shows"][0]["ids"]["simkl"] == 555
    assert await repo.get_show_by_id(created.id) is not None


@respx.mock
async def test_R2_an_edit_of_a_title_already_here_while_disconnected_is_not_held_and_goes_out(env):
    """Only titles added while disconnected are "add if missing". An edit of a title that was already here (from an
    earlier sync) goes out as usual once connected: an edit made while logged out is never dropped."""
    repo, _ = env
    client, sync = await early_env(repo, "off")
    show_id = await repo.upsert_show(Show(title="Severance", year=2022, media_type=MediaType.SHOW,
                                          status=ShowStatus.WATCHING, ids={"simkl": 555}))
    await sync.update_show_status(show_id, ShowStatus.HOLD)  # while disconnected (no token)
    [row] = await repo.get_outbox()
    assert not row["payload"].get("if_missing") and row["approved"]
    client.access_token = "tok"
    sent = capture_posts()
    assert await sync.flush_outbox() == 1
    assert sent[0][0] == "/sync/add-to-list" and sent[0][1]["shows"][0]["to"] == "hold"
    await client.aclose()


# ------------------------------------------------------------------------------------------------------------- Q1-Q4

def mock_account(shows=(), anime=(), anime_status=200):
    respx.get(f"{SIMKL_BASE_URL}/sync/activities").respond(json=ACTIVITIES_1)
    respx.get(f"{SIMKL_BASE_URL}/sync/all-items/shows").respond(json={"shows": list(shows)})
    respx.get(f"{SIMKL_BASE_URL}/sync/all-items/anime").respond(anime_status, json={"anime": list(anime)})
    respx.get(f"{SIMKL_BASE_URL}/sync/all-items/movies").respond(json={"movies": []})
    respx.get(url__regex=rf"{SIMKL_BASE_URL}/(tv|movies|anime)/\d+$").respond(json={})
    respx.get(f"{SIMKL_BASE_URL}/tv/episodes/1400298").respond(json=[])


@pytest.mark.parametrize("review", ["off", "on"])
@respx.mock
async def test_Q1_a_rating_given_before_the_login_is_kept_for_the_person_when_the_account_has_the_title(env, review):
    repo, _ = env
    client, sync = await early_env(repo, review)
    created = await add_early(repo, sync, "Severance")
    await sync.update_show_rating(created.id, 10)
    assert [(r["op"], r["payload"]["if_missing"]) for r in await repo.get_outbox()] == [("status", "add"), ("rate", "edit")]
    client.access_token = "tok"
    mock_reads(shows=[SEVERANCE], movies=[])  # the account: rated 9
    sent = capture_posts()
    assert (await sync.sync(deep=False))["status"] == "success"
    await sync.flush_outbox()
    assert sent == []  # nothing goes out unasked
    [row] = await repo.get_outbox()  # the add is gone, the rating waits for a decision
    assert (row["op"], row["approved"], row["operation"].rating, row["payload"].get("if_missing")) == ("rate", False, 10, None)
    if review == "on":
        await sync.resolve_incoming(None, True)
    assert (await repo.get_show_by_id(created.id)).user_rating == 9  # the account's version came in
    if review == "off":  # approve: the 10 is sent
        await sync.approve_outgoing()
        assert await sync.flush_outbox() == 1 and sent[0][0] == "/sync/ratings"
    else:  # decline: the account's 9 stays
        out = await sync.discard_outgoing([row["id"]])
        assert out["discarded"] == 1 and await repo.get_outbox() == []
        assert (await repo.get_show_by_id(created.id)).user_rating == 9
    await client.aclose()


@respx.mock
async def test_Q2_a_title_the_catalogue_calls_a_show_and_the_account_files_as_anime_counts_as_on_the_account(env):
    repo, _ = env
    client, sync = await early_env(repo, "off")
    created = await add_early(repo, sync, "Severance")
    client.access_token = "tok"
    mock_account(anime=[SEVERANCE])
    sent = capture_posts()
    assert (await sync.sync(deep=False))["status"] == "success"
    await sync.flush_outbox()
    assert sent == []  # the add was not sent: the account's Watching title stays Watching
    assert await repo.get_outbox() == []
    assert (await repo.get_show_by_id(created.id)).status == ShowStatus.WATCHING
    await client.aclose()


@respx.mock
async def test_Q1_a_pull_that_fails_in_a_later_media_type_leaves_every_held_row_and_the_cursor_alone(env):
    repo, _ = env
    client, sync = await early_env(repo, "off")
    created = await add_early(repo, sync, "Severance")
    await sync.update_show_rating(created.id, 10)
    before = [(r["id"], r["approved"], r["payload"]) for r in await repo.get_outbox()]
    client.access_token = "tok"
    mock_account(shows=[SEVERANCE], anime_status=503)  # the shows listing runs and shows the title; anime fails
    sent = capture_posts()
    result = await sync.sync(deep=False)
    assert result["status"] == "error"
    await sync.flush_outbox()
    assert sent == []
    assert [(r["id"], r["approved"], r["payload"]) for r in await repo.get_outbox()] == before  # untouched
    assert await sync.get_state("cursor") is None
    await client.aclose()


@respx.mock
async def test_Q3_a_held_row_makes_the_next_due_pull_a_full_one_but_not_every_wake(env):
    from penguwatch.sync.worker import worker_step
    repo, _ = env
    client, sync = await early_env(repo, "off")
    await add_early(repo, sync, "Severance")
    client.access_token = "tok"
    activities = respx.get(f"{SIMKL_BASE_URL}/sync/activities").respond(503)
    respx.get(url__startswith=SIMKL_BASE_URL).respond(503)
    for _ in range(3):  # a push-only wake: edits, the short tick
        assert await worker_step(sync, pull_due=False) == "flushed"
    assert activities.call_count == 0  # no full sync at all
    assert await worker_step(sync, pull_due=True) == "pulled"  # due: one pull (it fails here: the normal back-off)
    seen = activities.call_count
    assert seen >= 1
    for _ in range(3):
        await worker_step(sync, pull_due=False)
    assert activities.call_count == seen  # and again nothing until the next due pull
    assert await repo.has_flagged_outbox("simkl")
    await client.aclose()


@respx.mock
async def test_Q4_rows_that_cannot_be_declined_can_be_dropped_without_restoring(env):
    repo, client = env
    mock_reads(shows=[SEVERANCE], movies=[])
    sync = simkl_engine(client, repo)
    await sync.sync(deep=False)
    await repo.set_state("sync_review_mode", "on")
    sev = await repo.get_show_by_external_id("simkl", 1400298)
    await sync.update_show_rating(sev.id, 3)
    await repo.enqueue_outbox("gone", op(OP_STATUS, status="hold"), None, 9, approved=False)  # a driver that is gone
    client.access_token = None  # disconnected
    ids = [r["id"] for r in await repo.get_outbox()]
    review = await get_sync_review(repo, sync)
    assert all(o["discard_blocked"] for o in review["outgoing"])
    with pytest.raises(HTTPException) as refused:  # a plain discard is still refused
        await discard_outgoing(OutgoingDiscardRequest(ids=ids), sync)
    assert refused.value.status_code == 409 and len(await repo.get_outbox()) == 2

    calls = len(respx.calls)
    out = await discard_outgoing(OutgoingDiscardRequest(ids=ids, restore=False), sync)
    assert out == {"discarded": 2, "restored": 0, "removed": 0}
    assert await repo.get_outbox() == []
    assert len(respx.calls) == calls  # nothing fetched
    assert (await repo.get_show_by_id(sev.id)).user_rating == 3  # the local state stays as it is
