"""A request waits only a few seconds for a slow catalogue; it answers from what is stored and the fetch finishes later."""

import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch.api.deps import install
from penguwatch.db.repository import DatabaseRepository
from penguwatch.drivers.simkl.client import SimklClient
from penguwatch.models import MediaType, ServiceConfig, Show, ShowStatus
from penguwatch.main import app
from penguwatch.sync import enrich
from tests.simkl_helpers import simkl_engine


@pytest.fixture
async def env(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "deadline.db"))
    await repo.initialize([ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99)])
    client = SimklClient(client_id="cid", access_token="tok")
    sync = simkl_engine(client, repo)
    yield repo, client, sync
    await client.aclose()
    await repo.close()


async def slow_catalogue(client, gate, calls):
    async def get_episodes(media, simkl_id):
        calls.append(simkl_id)
        await gate.wait()  # a catalogue that does not answer until the test lets it
        return [{"type": "episode", "season": 1, "episode": n, "title": f"E{n}", "date": "2026-01-0%dT00:00:00Z" % n}
                for n in (1, 2, 3)]
    client.get_episodes = get_episodes


async def new_show(repo, **fields):
    sid = await repo.upsert_show(Show(ids={"simkl": 77}, title="Slow", media_type=MediaType.SHOW, status=ShowStatus.WATCHING,
                                      watched_episodes=1, aired_episodes=3, total_episodes=3))
    if fields:
        await repo.update_show_fields(sid, fields)
    return sid


@pytest.mark.asyncio
async def test_a_slow_catalogue_does_not_hold_up_a_request(env):
    repo, client, sync = env
    gate, calls = asyncio.Event(), []
    await slow_catalogue(client, gate, calls)
    show = await repo.get_show_by_id(await new_show(repo))
    t0 = time.perf_counter()
    assert await sync.ensure_episode_list(show, deadline=0.1) is False  # nothing stored yet
    assert time.perf_counter() - t0 < 1.0 and calls == [77]
    assert (await repo.get_show_by_id(show.id)).episodes_synced_at is None
    gate.set()  # the catalogue answers late: the fetch that was left running stores the list
    for _ in range(100):
        await asyncio.sleep(0.02)
        if (await repo.get_show_by_id(show.id)).episodes_synced_at:
            break
    assert (await repo.get_show_by_id(show.id)).episodes_synced_at
    assert len(await repo.get_episodes(show.id)) == 3
    assert not enrich._LATE_REFRESHES


@pytest.mark.asyncio
async def test_a_slow_refresh_of_a_stored_list_answers_with_the_stored_one(env):
    repo, client, sync = env
    sid = await new_show(repo)
    await repo.upsert_episode_list(sid, [{"season": 1, "number": 1, "title": "Old", "air_date": "2025-01-01"}])
    await repo.update_show_fields(sid, {"episodes_synced_at": "2020-01-01T00:00:00+00:00"})  # stale: due a refresh
    gate, calls = asyncio.Event(), []
    await slow_catalogue(client, gate, calls)
    show = await repo.get_show_by_id(sid)
    assert await sync.ensure_episode_list(show, deadline=0.05) is True  # a stored list exists
    gate.set()
    await asyncio.sleep(0.2)


@pytest.mark.asyncio
async def test_the_episodes_endpoint_answers_from_the_stored_list_within_the_deadline(env, monkeypatch):
    repo, client, sync = env
    monkeypatch.setattr(enrich, "REQUEST_DEADLINE_S", 0.1)
    sid = await new_show(repo)
    await repo.upsert_episode_list(sid, [{"season": 1, "number": 1, "title": "Stored", "air_date": "2025-01-01"}])
    gate, calls = asyncio.Event(), []
    await slow_catalogue(client, gate, calls)
    from penguwatch.api.shows import list_episodes
    t0 = time.perf_counter()
    body = await list_episodes(sid, refresh=False, repo=repo, sync=sync)
    assert time.perf_counter() - t0 < 2.0 and calls == [77]
    assert [e["title"] for e in body["seasons"][0]["episodes"]] == ["Stored"]
    assert body["complete_list"] is False
    gate.set()
    await asyncio.sleep(0.3)
    body = await list_episodes(sid, refresh=False, repo=repo, sync=sync)
    assert body["complete_list"] is True and len(body["seasons"][0]["episodes"]) == 3  # the next look has the full list
