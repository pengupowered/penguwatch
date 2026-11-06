"""Pins the exact dict returned by SimklSyncService._pull for full, delta and staged runs."""

import json

import pytest
import respx

from penguwatch.drivers.simkl.client import SIMKL_BASE_URL
from penguwatch.models import ShowStatus

from tests.test_two_way_sync import ACTIVITIES_1, SEVERANCE, mock_reads, env  # noqa: F401  (env fixture)


def _acts(**tv):
    a = json.loads(json.dumps(ACTIVITIES_1))
    a["all"] = a["tv_shows"]["all"] = "2026-10-05T10:00:00Z"
    a["tv_shows"].update(tv)
    return a


def _remote_hold():
    return dict(SEVERANCE, status="hold", user_rating=7, watched_episodes_count=3,
                seasons=[{"number": 1, "episodes": [{"number": n} for n in (1, 2, 3)]}])


@pytest.mark.asyncio
@respx.mock
async def test_pull_result_full(env):
    repo, client, sync = env
    mock_reads()
    result = await sync._pull(False)
    sev = await repo.get_show_by_external_id("simkl", 1400298)
    movie = await repo.get_show_by_external_id("simkl", 2222)
    assert result == {"mode": "full", "updated": 2, "removed": 0, "touched_ids": [sev.id, movie.id]}
    assert json.loads(await repo.get_state("driver.simkl.cursor"))["all"] == ACTIVITIES_1["all"]
    assert await repo.get_state("incoming_staged") is None


@pytest.mark.asyncio
@respx.mock
async def test_pull_result_delta_list_change(env):
    repo, client, sync = env
    mock_reads()
    await sync._pull(False)
    respx.get(f"{SIMKL_BASE_URL}/sync/activities").respond(json=_acts(watching="2026-10-05T10:00:00Z"))
    respx.get(f"{SIMKL_BASE_URL}/sync/all-items/shows").respond(json={"shows": [_remote_hold()]})
    result = await sync._pull(False)
    sev = await repo.get_show_by_external_id("simkl", 1400298)
    assert result == {"mode": "delta", "updated": 1, "removed": 0, "touched_ids": [sev.id]}
    assert sev.status == ShowStatus.HOLD
    assert json.loads(await repo.get_state("driver.simkl.cursor"))["all"] == "2026-10-05T10:00:00Z"


@pytest.mark.asyncio
@respx.mock
async def test_pull_result_delta_rating_only(env):
    repo, client, sync = env
    mock_reads()
    await sync._pull(False)
    respx.get(f"{SIMKL_BASE_URL}/sync/activities").respond(json=_acts(rated_at="2026-10-05T10:00:00Z"))
    respx.get(f"{SIMKL_BASE_URL}/sync/ratings/shows").respond(
        json={"shows": [{"user_rating": 4, "show": SEVERANCE["show"]}]})
    result = await sync._pull(False)
    assert result == {"mode": "delta", "updated": 0, "removed": 0, "touched_ids": []}
    assert (await repo.get_show_by_external_id("simkl", 1400298)).user_rating == 4


@pytest.mark.asyncio
@respx.mock
async def test_pull_result_staged_keeps_old_snapshot(env):
    repo, client, sync = env
    mock_reads()
    await sync._pull(False)
    await repo.set_state("sync_review_mode", "on")
    respx.get(f"{SIMKL_BASE_URL}/sync/activities").respond(json=_acts(watching="2026-10-05T10:00:00Z"))
    respx.get(f"{SIMKL_BASE_URL}/sync/all-items/shows").respond(json={"shows": [_remote_hold()]})
    result = await sync._pull(False)
    assert result == {"mode": "delta", "updated": 0, "removed": 0, "touched_ids": [], "staged": 1}
    assert json.loads(await repo.get_state("driver.simkl.cursor"))["all"] == ACTIVITIES_1["all"]
    staged = json.loads(await repo.get_state("incoming_staged"))
    assert staged["cursor"]["all"] == "2026-10-05T10:00:00Z"
    assert [e["key"] for e in staged["entries"]] == ["show:1400298"]
    assert staged["entries"][0]["kind"] == "update"
    assert staged["entries"][0].get("rating_only") is None


@pytest.mark.asyncio
@respx.mock
async def test_pull_result_staged_rating_only_and_merge(env):
    repo, client, sync = env
    mock_reads()
    await sync._pull(False)
    await repo.set_state("sync_review_mode", "on")

    # rating-only change: staged as a rating_only entry
    respx.get(f"{SIMKL_BASE_URL}/sync/activities").respond(json=_acts(rated_at="2026-10-05T10:00:00Z"))
    respx.get(f"{SIMKL_BASE_URL}/sync/ratings/shows").respond(
        json={"shows": [{"user_rating": 4, "show": SEVERANCE["show"]}]})
    result = await sync._pull(False)
    assert result == {"mode": "delta", "updated": 0, "removed": 0, "touched_ids": [], "staged": 1}
    entry = json.loads(await repo.get_state("incoming_staged"))["entries"][0]
    assert entry["rating_only"] is True
    assert entry["changes"] == [{"field": "rating", "local": 9, "remote": 4}]

    # list change plus rating change on the same title: the full entry wins the merge
    respx.get(f"{SIMKL_BASE_URL}/sync/activities").respond(
        json=_acts(watching="2026-10-05T10:00:00Z", rated_at="2026-10-05T10:00:00Z"))
    respx.get(f"{SIMKL_BASE_URL}/sync/all-items/shows").respond(json={"shows": [_remote_hold()]})
    respx.get(f"{SIMKL_BASE_URL}/sync/ratings/shows").respond(
        json={"shows": [{"user_rating": 7, "show": SEVERANCE["show"]}]})
    result = await sync._pull(False)
    assert result == {"mode": "delta", "updated": 0, "removed": 0, "touched_ids": [], "staged": 1}
    entry = json.loads(await repo.get_state("incoming_staged"))["entries"][0]
    assert entry.get("rating_only") is None and entry["kind"] == "update"
