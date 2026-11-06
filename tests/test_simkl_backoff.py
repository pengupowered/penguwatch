"""24 h back-off for background refreshes that fail or come back empty (no schema change: app_state)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from penguwatch.models import MediaType, ShowStatus
from penguwatch.sync import backoff, enrich

from tests.test_simkl_politeness import add_shows, env, no_spacing  # noqa: F401  (fixtures + helper)

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------ pure module

def test_load_tolerates_missing_and_corrupt_values():
    assert backoff.load(None) == {}
    assert backoff.load("") == {}
    assert backoff.load("{not json") == {}
    assert backoff.load("[1, 2]") == {}
    assert backoff.load(json.dumps({"episodes:1": T0.isoformat(), "bad": 5})) == {"episodes:1": T0.isoformat()}
    assert backoff.load(backoff.dump({"a": "b"})) == {"a": "b"}


def test_due_record_clear_and_prune():
    a = backoff.record({}, "episodes:1", T0)
    assert backoff.due({}, "episodes:1", T0)
    assert not backoff.due(a, "episodes:1", T0 + timedelta(hours=23, minutes=59))
    assert backoff.due(a, "episodes:1", T0 + timedelta(hours=24))
    assert backoff.due(a, "episodes:2", T0)  # other keys unaffected
    assert backoff.due({"k": "garbage"}, "k", T0)  # unparsable entries never block
    assert backoff.clear(a, "episodes:1") == {}
    old = backoff.record(a, "sequels:9", T0 - timedelta(days=15))
    assert set(backoff.prune(old, T0)) == {"episodes:1"}
    assert backoff.prune({"k": "garbage"}, T0) == {}


# ------------------------------------------------------------ integration through the refresh loops

class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(enrich, "_utcnow", c)
    return c


async def attempts_of(repo):
    return backoff.load(await repo.get_state(backoff.STATE_KEY))


@pytest.mark.asyncio
async def test_empty_episode_list_is_skipped_then_retried_after_24h(env, no_spacing, clock):
    repo, client, sync = env
    (sid,) = await add_shows(repo, 1, status=ShowStatus.WATCHING, media_type=MediaType.SHOW)
    calls = []

    async def get_episodes(media, simkl_id):
        calls.append(simkl_id)
        return []

    client.get_episodes = get_episodes

    assert await sync.refresh_episode_lists() == 1 and len(calls) == 1
    assert list(await attempts_of(repo)) == [f"episodes:{sid}"]
    clock.now += timedelta(hours=23)
    assert await sync.refresh_episode_lists() == 0 and len(calls) == 1  # still backed off
    clock.now += timedelta(hours=1)
    assert await sync.refresh_episode_lists() == 1 and len(calls) == 2  # retried


@pytest.mark.asyncio
async def test_failing_titles_do_not_block_others_under_the_cap(env, no_spacing, clock):
    repo, client, sync = env
    bad = await add_shows(repo, 3, first_id=100, status=ShowStatus.WATCHING, media_type=MediaType.SHOW)
    good = await add_shows(repo, 3, first_id=200, status=ShowStatus.WATCHING, media_type=MediaType.SHOW)
    tried = []

    async def get_episodes(media, simkl_id):
        tried.append(simkl_id)
        if simkl_id < 200:
            raise RuntimeError("boom")
        return [{"season": 1, "episode": 1, "type": "episode", "title": "E1", "date": "2020-01-01"}]

    client.get_episodes = get_episodes

    await sync.refresh_episode_lists(limit=3)
    await sync.refresh_episode_lists(limit=3)
    assert await sync.refresh_episode_lists(limit=3) == 0  # everything succeeded or is backed off
    assert sorted(tried) == [100, 101, 102, 200, 201, 202]  # each exactly once
    for sid in good:
        assert (await repo.get_show_by_id(sid)).episodes_synced_at
    assert set(await attempts_of(repo)) == {f"episodes:{i}" for i in bad}


@pytest.mark.asyncio
async def test_success_clears_the_key_and_state(env, no_spacing, clock):
    repo, client, sync = env
    await add_shows(repo, 1, status=ShowStatus.WATCHING, media_type=MediaType.SHOW)
    answers = [[], [{"season": 1, "episode": 1, "type": "episode", "title": "E1", "date": "2020-01-01"}]]

    async def get_episodes(media, simkl_id):
        return answers.pop(0)

    client.get_episodes = get_episodes
    await sync.refresh_episode_lists()
    assert await attempts_of(repo)
    clock.now += timedelta(hours=25)
    await sync.refresh_episode_lists()
    assert await attempts_of(repo) == {}
    assert await repo.get_state(backoff.STATE_KEY) is None


@pytest.mark.asyncio
async def test_corrupt_stored_value_is_treated_as_empty(env, no_spacing, clock):
    repo, client, sync = env
    (sid,) = await add_shows(repo, 1, status=ShowStatus.WATCHING, media_type=MediaType.SHOW)
    await repo.set_state(backoff.STATE_KEY, "{definitely not json")

    async def get_episodes(media, simkl_id):
        return []

    client.get_episodes = get_episodes
    assert await sync.refresh_episode_lists() == 1
    assert list(await attempts_of(repo)) == [f"episodes:{sid}"]  # rewritten as valid JSON


@pytest.mark.asyncio
async def test_state_is_written_once_per_cycle(env, no_spacing, clock):
    repo, client, sync = env
    await add_shows(repo, 5, status=ShowStatus.WATCHING, media_type=MediaType.SHOW)
    writes = []
    real = repo.set_state

    async def spy(key, value):
        if key == backoff.STATE_KEY:
            writes.append(value)
        await real(key, value)

    repo.set_state = spy

    async def get_episodes(media, simkl_id):
        return []

    client.get_episodes = get_episodes
    await sync.refresh_episode_lists()
    assert len(writes) == 1 and len(json.loads(writes[0])) == 5


@pytest.mark.asyncio
async def test_user_triggered_refreshes_ignore_the_back_off(env, no_spacing, clock):
    repo, client, sync = env
    (sid,) = await add_shows(repo, 1, status=ShowStatus.WATCHING, media_type=MediaType.SHOW)
    recent = backoff.dump({f"episodes:{sid}": T0.isoformat(), f"enrich:{sid}": T0.isoformat()})
    await repo.set_state(backoff.STATE_KEY, recent)
    episode_calls, detail_calls = [], []

    async def get_episodes(media, simkl_id):
        episode_calls.append(simkl_id)
        return []

    async def get_details(media, simkl_id):
        detail_calls.append(simkl_id)
        return {"overview": "x"}

    client.get_episodes, client.get_details = get_episodes, get_details
    show = await repo.get_show_by_id(sid)

    # background loops skip it
    assert await sync.refresh_episode_lists() == 0
    assert await sync.enrich_titles() == 0
    assert episode_calls == [] and detail_calls == []
    # a click (ensure_episode_list) and an explicit enrich do not
    await sync.ensure_episode_list(show)
    assert await sync.enrich_titles([sid]) == 1
    assert len(episode_calls) == 1 and len(detail_calls) == 1


@pytest.mark.asyncio
async def test_sequel_and_enrich_failures_back_off(env, no_spacing, clock):
    repo, client, sync = env
    (anime,) = await add_shows(repo, 1, status=ShowStatus.COMPLETED, media_type=MediaType.ANIME)
    calls = []

    async def get_details(media, simkl_id):
        calls.append(simkl_id)
        raise RuntimeError("down")

    client.get_details = get_details
    assert await sync.refresh_sequels() == 1
    assert await sync.refresh_sequels() == 0
    assert await sync.enrich_titles() == 1  # fails quietly inside _fetch_details -> empty -> back-off
    assert set(await attempts_of(repo)) == {f"sequels:{anime}", f"enrich:{anime}"}
    clock.now += timedelta(hours=24)
    assert await sync.refresh_sequels() == 1
