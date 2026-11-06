"""A long outbox drains in one worker step (rounds of 50 due rows), with each title's order, the hold points and the
review rules kept."""

import json
from datetime import timedelta

import httpx
import pytest
import respx

from penguwatch.sync import push
from penguwatch.sync.enrich import _iso, _utcnow
from penguwatch.sync.worker import worker_step
from tests.test_outbox_batching import HIST, ep, env, queue, sids  # noqa: F401  (env is a fixture)


def episodes_of(call):
    """{title id: [episode numbers]} of one history request."""
    out = {}
    for show in json.loads(call.request.content)["shows"]:
        for season in show["seasons"]:
            out.setdefault(show["ids"]["simkl"], []).extend(e["number"] for e in season["episodes"])
    return out


async def queue_long_outbox(repo):
    """250 titles with two episodes each (500 approved rows), 20 rows waiting for approval, and one title whose first
    row is backing off after a failed delivery, so its newer rows must wait."""
    for number in (1, 2):
        for sid in range(1, 251):
            await queue(repo, "history_add", sid, ep(sid, number))
    for sid in range(1001, 1021):
        await queue(repo, "history_add", sid, ep(sid), approved=False)
    first = await queue(repo, "history_add", 2001, ep(2001, 1))
    await queue(repo, "history_add", 2001, ep(2001, 2))
    await repo.fail_outbox(first, "503", _iso(_utcnow() + timedelta(hours=1)))


@pytest.mark.asyncio
@respx.mock
async def test_500_queued_rows_drain_in_one_worker_step(env):  # noqa: F811
    repo, _, sync = env
    await queue_long_outbox(repo)
    route = respx.post(HIST).respond(json={})
    assert await worker_step(sync, pull_due=False) == "flushed"
    delivered = {}
    for call in route.calls:
        for sid, numbers in episodes_of(call).items():
            delivered.setdefault(sid, []).extend(numbers)
    assert sorted(delivered) == list(range(1, 251))  # every approved title, nothing else
    assert all(numbers == [1, 2] for numbers in delivered.values())  # each title's rows went out in order
    left = await repo.get_outbox()
    assert sorted(r["remote_id"] for r in left) == sorted([*range(1001, 1021), 2001, 2001])  # waiting + held
    assert {r["remote_id"] for r in left if r["approved"]} == {2001}  # the held title still has both its rows
    assert not any(r["remote_id"] in range(1001, 1021) for call in route.calls for r in [{"remote_id": s} for s in sids(json.loads(call.request.content))])


@pytest.mark.asyncio
@respx.mock
async def test_a_flush_stops_after_max_rounds_and_the_next_one_continues(env, monkeypatch):  # noqa: F811
    repo, _, sync = env
    monkeypatch.setattr(push, "MAX_ROUNDS", 2)
    for sid in range(1, 131):
        await queue(repo, "history_add", sid, ep(sid))
    respx.post(HIST).respond(json={})
    assert await sync.flush_outbox() == 100  # two reads of 50
    assert len(await repo.get_outbox()) == 30
    assert await sync.flush_outbox() == 30
    assert await repo.get_outbox() == []


@pytest.mark.asyncio
@respx.mock
async def test_a_failure_in_one_round_holds_that_titles_later_rows_in_the_next(env):  # noqa: F811
    repo, _, sync = env
    # title 1's first row is in the first read of 50, its second row in the second read
    await queue(repo, "history_add", 1, ep(1, 1))
    for sid in range(2, 61):
        await queue(repo, "history_add", sid, ep(sid))
    second = await queue(repo, "history_add", 1, ep(1, 2))
    # a request carrying title 1 fails (the whole batch of 25 is backed off); the others are accepted
    respx.post(HIST).mock(side_effect=lambda r: httpx.Response(503) if 1 in sids(json.loads(r.content)) else httpx.Response(200, json={}))
    assert await sync.flush_outbox() == 35  # titles 26..60: the batch with titles 1..25 was backed off
    left = {r["id"]: r for r in await repo.get_outbox()}
    assert sorted(r["remote_id"] for r in left.values()) == sorted([*range(1, 26), 1])
    assert left[second]["attempts"] == 0  # not sent ahead of the older row of the same title, though it came up in a later read
    assert all(r["attempts"] == 1 for i, r in left.items() if i != second)


@pytest.mark.asyncio
@respx.mock
async def test_hold_points_are_read_again_every_round(env):  # noqa: F811
    repo, _, sync = env
    for sid in range(1, 61):  # the first read of 50 takes titles 1..50
        await queue(repo, "history_add", sid, ep(sid))
    older = await queue(repo, "history_add", 900, ep(900, 1))
    newer = await queue(repo, "history_add", 900, ep(900, 2))
    original = repo.outbox_hold_points
    reads = []

    async def holds_after_a_failure_elsewhere(driver):
        if reads:  # between the rounds title 900's older row fails (as a delivery by another path could)
            await repo.fail_outbox(older, "503", _iso(_utcnow() + timedelta(hours=1)))
        reads.append(1)
        return await original(driver)
    repo.outbox_hold_points = holds_after_a_failure_elsewhere
    route = respx.post(HIST).respond(json={})
    assert await sync.flush_outbox() == 60
    assert 900 not in [s for c in route.calls for s in sids(json.loads(c.request.content))]
    assert sorted(r["id"] for r in await repo.get_outbox()) == [older, newer]
