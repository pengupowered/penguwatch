"""Outbox delivery batches rows of different titles into one SIMKL request and keeps each title's order."""

import json

import httpx
import pytest
import respx

from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import ServiceConfig
from penguwatch.drivers.simkl import auth as auth_state
from penguwatch.sync import push
from penguwatch.drivers.simkl.client import SIMKL_BASE_URL, SimklClient
from penguwatch.drivers.base import SyncOp
from penguwatch.drivers.simkl.payloads import batch_key as payload_batch_key, merge_payloads
from tests.simkl_helpers import simkl_engine

HIST = f"{SIMKL_BASE_URL}/sync/history"
HIST_REMOVE = f"{SIMKL_BASE_URL}/sync/history/remove"
LIST = f"{SIMKL_BASE_URL}/sync/add-to-list"
RATE = f"{SIMKL_BASE_URL}/sync/ratings"


@pytest.fixture
async def env(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "batch.db"))
    await repo.initialize([ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99, aliases=["Apple TV+"])])
    client = SimklClient(client_id="cid", access_token="tok")
    client.retry_base_delay = 0
    sync = simkl_engine(client, repo)
    yield repo, client, sync
    await client.aclose()
    await repo.close()


def ep(sid, number=1):
    return {"shows": [{"ids": {"simkl": sid}, "seasons": [{"number": 1, "episodes": [{"number": number}]}]}]}


def movie(sid, **extra):
    return {"movies": [{"ids": {"simkl": sid}, **extra}]}


def move(sid, status):
    return {"to": status, "shows": [{"ids": {"simkl": sid}, "to": status}]}


# The tests below name rows by SIMKL's operations and bodies, as the outbox held them before the driver layer; such a
# row is stored as a neutral op that carries the body as `native` (what migration 0016 does to existing rows).
KINDS = {"history_add": "watched", "history_remove": "unwatched", "list_move": "status", "rating_set": "rate",
         "rating_remove": "unrate", "remove_item": "remove"}


def op_for(op, sid, payload):
    media = "movie" if "movies" in payload else "show"
    return SyncOp(kind=KINDS.get(op, op), media_type=media, ids={"simkl": sid}, native=payload)


async def queue(repo, op, sid, payload, approved=True):
    return await repo.enqueue_outbox("simkl", op_for(op, sid, payload), None, sid, approved=approved)


def body(call):
    return json.loads(call.request.content)


def sids(payload):
    return [x["ids"]["simkl"] for bucket in ("shows", "movies") for x in payload.get(bucket, [])]


# --- pure helpers -----------------------------------------------------------------------------

def test_merge_payloads_concatenates_lists_and_keeps_shared_scalars():
    merged = merge_payloads([move(1, "hold"), move(2, "hold"), movie(3)])
    assert merged["to"] == "hold"
    assert sids(merged) == [1, 2, 3]
    assert [x["to"] for x in merged["shows"]] == ["hold", "hold"]


def test_merge_payloads_does_not_mutate_inputs():
    a, b = ep(1), ep(2)
    merge_payloads([a, b])
    assert len(a["shows"]) == 1 and len(b["shows"]) == 1


def test_merge_payloads_rejects_mismatched_scalars():
    with pytest.raises(ValueError):
        merge_payloads([move(1, "hold"), move(2, "dropped")])
    with pytest.raises(ValueError):
        merge_payloads([{"shows": []}, {"shows": "x"}])


def test_batch_key_groups_by_endpoint_and_target_status():
    row = lambda op, payload, i=1: {"id": i, "operation": op_for(op, i, payload)}  # noqa: E731

    def batch_key(r):  # what the engine groups by: the driver's key, a lone op its own row
        key = payload_batch_key(r["operation"])
        return ("single", r["id"]) if key is None else key
    assert batch_key(row("remove_item", movie(1))) == batch_key(row("history_remove", ep(2)))
    assert batch_key(row("history_add", ep(1))) != batch_key(row("history_remove", ep(1)))
    assert batch_key(row("list_move", move(1, "hold"))) != batch_key(row("list_move", move(1, "dropped")))
    assert batch_key(row("rating_set", movie(1, rating=5))) != batch_key(row("rating_remove", movie(1)))
    assert batch_key(row("mystery", {}, 1)) != batch_key(row("mystery", {}, 2))


# --- flush ------------------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_same_op_for_three_titles_is_one_request(env):
    repo, _, sync = env
    for sid in (1, 2, 3):
        await queue(repo, "history_add", sid, ep(sid))
    route = respx.post(HIST).respond(json={"added": {"episodes": 3}})
    assert await sync.flush_outbox() == 3
    assert route.call_count == 1
    assert sids(body(route.calls[0])) == [1, 2, 3]
    assert await repo.get_outbox() == []


@pytest.mark.asyncio
@respx.mock
async def test_titles_keep_their_order_across_waves(env):
    repo, _, sync = env
    await queue(repo, "history_add", 1, ep(1))          # A add
    await queue(repo, "rating_set", 1, movie(1, rating=8))  # A rate
    await queue(repo, "rating_set", 2, movie(2, rating=6))  # B rate
    calls = []
    respx.post(HIST).mock(side_effect=lambda r: (calls.append(("add", json.loads(r.content))), httpx.Response(200, json={}))[1])
    respx.post(RATE).mock(side_effect=lambda r: (calls.append(("rate", json.loads(r.content))), httpx.Response(200, json={}))[1])
    assert await sync.flush_outbox() == 3
    assert [c[0] for c in calls] == ["add", "rate"]
    assert sids(calls[0][1]) == [1]
    assert sids(calls[1][1]) == [1, 2]


@pytest.mark.asyncio
@respx.mock
async def test_remove_item_and_history_remove_share_one_request(env):
    repo, _, sync = env
    await queue(repo, "remove_item", 1, movie(1))
    await queue(repo, "history_remove", 2, ep(2))
    route = respx.post(HIST_REMOVE).respond(json={})
    assert await sync.flush_outbox() == 2
    assert route.call_count == 1 and sorted(sids(body(route.calls[0]))) == [1, 2]
    assert [list(body(route.calls[0])[b][0]['ids'].values()) for b in ('movies', 'shows')] == [[1], [2]]


@pytest.mark.asyncio
@respx.mock
async def test_list_moves_batch_only_within_the_same_target_status(env):
    repo, _, sync = env
    await queue(repo, "list_move", 1, move(1, "hold"))
    await queue(repo, "list_move", 2, move(2, "dropped"))
    await queue(repo, "list_move", 3, move(3, "hold"))
    route = respx.post(LIST).respond(json={})
    assert await sync.flush_outbox() == 3
    assert route.call_count == 2
    first, second = body(route.calls[0]), body(route.calls[1])
    assert first["to"] == "hold" and sids(first) == [1, 3] and all(x["to"] == "hold" for x in first["shows"])
    assert second["to"] == "dropped" and sids(second) == [2]


@pytest.mark.asyncio
@respx.mock
async def test_requests_are_capped_at_max_batch(env, monkeypatch):
    repo, _, sync = env
    monkeypatch.setattr(push, "MAX_BATCH", 5)
    for sid in range(1, 13):
        await queue(repo, "history_add", sid, ep(sid))
    route = respx.post(HIST).respond(json={})
    assert await sync.flush_outbox() == 12
    assert [len(sids(body(c))) for c in route.calls] == [5, 5, 2]


@pytest.mark.asyncio
@respx.mock
async def test_fifty_rows_take_two_requests_by_default(env):
    repo, _, sync = env
    for sid in range(1, 51):
        await queue(repo, "history_add", sid, ep(sid))
    route = respx.post(HIST).respond(json={})
    assert await sync.flush_outbox() == 50
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_one_title_is_never_twice_in_a_request(env):
    repo, _, sync = env
    await queue(repo, "history_add", 1, ep(1, 1))
    await queue(repo, "history_add", 1, ep(1, 2))
    await queue(repo, "history_add", 2, ep(2, 1))
    route = respx.post(HIST).respond(json={})
    assert await sync.flush_outbox() == 3
    assert [sids(body(c)) for c in route.calls] == [[1, 2], [1]]
    assert body(route.calls[1])["shows"][0]["seasons"][0]["episodes"] == [{"number": 2}]


@pytest.mark.asyncio
@respx.mock
async def test_transient_failure_backs_every_row_off_without_single_retries(env):
    repo, _, sync = env
    for sid in (1, 2, 3):
        await queue(repo, "history_add", sid, ep(sid))
    route = respx.post(HIST).respond(status_code=503)
    assert await sync.flush_outbox() == 0
    assert route.call_count == 3  # the client's own retries of the one batch request; no per-row calls
    assert all(sids(body(c)) == [1, 2, 3] for c in route.calls)
    rows = await repo.get_outbox()
    assert [r["attempts"] for r in rows] == [1, 1, 1]
    assert await repo.get_due_outbox("simkl") == []


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_400_is_transient_too(env):
    repo, _, sync = env
    for sid in (1, 2):
        await queue(repo, "history_add", sid, ep(sid))
    route = respx.post(HIST).respond(status_code=400, json={"error": "rate_limit"})
    assert await sync.flush_outbox() == 0
    assert {sids(body(c))[0] for c in route.calls} == {1} and all(len(sids(body(c))) == 2 for c in route.calls)
    assert [r["attempts"] for r in await repo.get_outbox()] == [1, 1]


@pytest.mark.asyncio
@respx.mock
async def test_rejected_batch_falls_back_to_one_by_one(env):
    repo, _, sync = env
    for sid in (1, 2, 3):
        await queue(repo, "history_add", sid, ep(sid))

    def answer(request):
        sent = sids(json.loads(request.content))
        if len(sent) > 1 or sent == [2]:
            return httpx.Response(400, json={"error": "invalid"})
        return httpx.Response(200, json={})

    route = respx.post(HIST).mock(side_effect=answer)
    assert await sync.flush_outbox() == 2
    assert route.call_count == 4  # 1 batch + 3 singles
    left = await repo.get_outbox()
    assert [r["remote_id"] for r in left] == [2] and left[0]["attempts"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_duplicate_in_a_batch_is_dropped_on_fallback(env):
    repo, _, sync = env
    await queue(repo, "history_add", 1, ep(1))
    await queue(repo, "history_add", 2, ep(2))

    def answer(request):
        sent = sids(json.loads(request.content))
        return httpx.Response(409 if sent == [1] else 200 if len(sent) == 1 else 409, json={})

    respx.post(HIST).mock(side_effect=answer)
    assert await sync.flush_outbox() == 1  # only the row that really went through counts
    assert await repo.get_outbox() == []


@pytest.mark.asyncio
@respx.mock
async def test_not_found_is_logged_and_rows_are_done(env, caplog):
    repo, _, sync = env
    await queue(repo, "history_add", 1, ep(1))
    await queue(repo, "history_add", 2, ep(2))
    respx.post(HIST).respond(json={"added": {"episodes": 1},
                                   "not_found": {"shows": [{"title": "Secret Show", "ids": {"simkl": 2}}]}})
    with caplog.at_level("WARNING", logger="penguwatch.sync"):
        assert await sync.flush_outbox() == 2
    assert "could not match 1 of the 2 titles" in caplog.text and "SIMKL ids: [2]" in caplog.text
    assert "Secret Show" not in caplog.text  # the log is exported to Loki: ids, never titles
    assert await repo.get_outbox() == []


@pytest.mark.asyncio
@respx.mock
async def test_read_only_token_stops_a_batch_and_keeps_rows(env):
    repo, _, sync = env
    for sid in (1, 2):
        await queue(repo, "history_add", sid, ep(sid))
    route = respx.post(HIST).respond(status_code=403, json={"error": "insufficient_scope"})
    assert await sync.flush_outbox() == 0
    assert route.call_count == 1
    assert "read-only" in await repo.get_state("driver.simkl.last_error")
    assert [r["attempts"] for r in await repo.get_outbox()] == [0, 0]


@pytest.mark.asyncio
@respx.mock
async def test_rejected_token_stops_a_batch_and_asks_to_reconnect(env):
    repo, _, sync = env
    for sid in (1, 2):
        await queue(repo, "history_add", sid, ep(sid))
    respx.post(HIST).respond(status_code=401, json={"error": "user_token_failed"})
    assert await sync.flush_outbox() == 0
    assert len(await repo.get_outbox()) == 2
    assert (await auth_state.read(repo))["auth_state_raw"] == auth_state.NEEDS_RECONNECT


@pytest.mark.asyncio
@respx.mock
async def test_backing_off_row_holds_back_newer_rows_of_its_title(env):
    repo, _, sync = env
    a1 = await queue(repo, "history_add", 1, ep(1, 1))
    await queue(repo, "history_add", 1, ep(1, 2))
    await queue(repo, "history_add", 2, ep(2, 1))
    await repo.fail_outbox(a1, "boom", "2999-01-01T00:00:00")
    route = respx.post(HIST).respond(json={})
    assert await sync.flush_outbox() == 1
    assert route.call_count == 1 and sids(body(route.calls[0])) == [2]
    assert sorted(r["id"] for r in await repo.get_outbox()) == [a1, a1 + 1]


@pytest.mark.asyncio
@respx.mock
async def test_failed_head_keeps_the_titles_later_rows_queued(env):
    repo, _, sync = env
    await queue(repo, "history_add", 1, ep(1))
    await queue(repo, "rating_set", 1, movie(1, rating=5))
    await queue(repo, "history_add", 2, ep(2))
    respx.post(HIST).respond(status_code=503)
    rate = respx.post(RATE).respond(json={})
    assert await sync.flush_outbox() == 0
    assert not rate.called  # A's rating must not overtake A's failed add
    assert len(await repo.get_outbox()) == 3


@pytest.mark.asyncio
@respx.mock
async def test_review_mode_unapproved_rows_are_not_sent_and_never_block_approved_ones(env):
    repo, _, sync = env
    pending = await queue(repo, "history_add", 1, ep(1), approved=False)
    await queue(repo, "rating_set", 1, movie(1, rating=5))  # approved: goes out although an older row awaits review
    await queue(repo, "history_add", 2, ep(2))
    add = respx.post(HIST).respond(json={})
    rate = respx.post(RATE).respond(json={})
    assert await sync.flush_outbox() == 2
    assert sids(body(add.calls[0])) == [2] and rate.call_count == 1
    assert [r["id"] for r in await repo.get_outbox()] == [pending]  # only the unapproved row is left

    await sync.approve_outgoing(None)
    assert await sync.flush_outbox() == 1
    assert sids(body(add.calls[1])) == [1]
    assert await repo.get_outbox() == []


@pytest.mark.asyncio
async def test_hold_points_list_only_rows_that_are_backing_off(env):
    repo, _, _ = env
    await queue(repo, "history_add", 1, ep(1), approved=False)  # awaiting review: not a hold point
    await queue(repo, "history_add", 1, ep(1, 2))
    b = await queue(repo, "history_add", 2, ep(2))
    await queue(repo, "history_add", 2, ep(2, 2))
    await queue(repo, "history_add", 3, ep(3))
    await repo.fail_outbox(b, "x", "2999-01-01T00:00:00")
    assert await repo.outbox_hold_points("simkl") == {2: b}


@pytest.mark.asyncio
@respx.mock
async def test_rows_backing_off_still_hold_the_titles_newer_approved_rows(env):
    repo, _, sync = env
    head = await queue(repo, "history_add", 1, ep(1))
    await queue(repo, "rating_set", 1, movie(1, rating=5))
    await queue(repo, "history_add", 2, ep(2))
    await repo.fail_outbox(head, "503", "2999-01-01T00:00:00")  # failed earlier, still backing off
    add = respx.post(HIST).respond(json={})
    rate = respx.post(RATE).respond(json={})
    assert await sync.flush_outbox() == 1
    assert sids(body(add.calls[0])) == [2] and not rate.called  # title 1 keeps its order


@pytest.mark.asyncio
@respx.mock
async def test_empty_not_found_lists_are_not_a_warning(env, caplog):
    # SIMKL answers every write with a not_found object; empty lists mean it found everything
    repo, _, sync = env
    await queue(repo, "history_add", 1, ep(1))
    await queue(repo, "history_add", 2, ep(2))
    respx.post(HIST).respond(json={"added": {"episodes": 2}, "not_found": {"movies": [], "shows": [], "episodes": []}})
    with caplog.at_level("WARNING", logger="penguwatch.sync"):
        assert await sync.flush_outbox() == 2
    assert "could not match" not in caplog.text
