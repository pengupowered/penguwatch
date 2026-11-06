"""Review mode: local changes wait for approval; SIMKL changes are staged until accepted or rejected."""

import json
import pytest
import respx

from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import ServiceConfig, ShowStatus
from penguwatch.drivers.simkl.client import SimklClient, SIMKL_BASE_URL
from tests.simkl_helpers import simkl_engine

from tests.test_two_way_sync import ACTIVITIES_1, SEVERANCE, MOVIE, mock_reads


@pytest.fixture
async def env(tmp_path):
    repo = DatabaseRepository(str(tmp_path / "review.db"))
    await repo.initialize([ServiceConfig(id="apple_tv", name="Apple TV+", monthly_cost=9.99, aliases=["Apple TV+"])])
    client = SimklClient(client_id="cid", access_token="tok")
    client.retry_base_delay = 0
    sync = simkl_engine(client, repo)
    yield repo, client, sync
    await client.aclose()
    await repo.close()


def changed_activities():
    a = json.loads(json.dumps(ACTIVITIES_1))
    a["all"] = a["tv_shows"]["all"] = a["tv_shows"]["watching"] = "2026-10-05T10:00:00Z"
    return a


@pytest.mark.asyncio
@respx.mock
async def test_local_changes_wait_for_approval(env):
    repo, client, sync = env
    mock_reads()
    await sync.sync(deep=False)  # first import into an empty library applies directly
    sev = await repo.get_show_by_external_id("simkl", 1400298)
    assert sev is not None

    await sync.update_show_status(sev.id, ShowStatus.HOLD)
    out = await repo.get_outbox()
    assert len(out) == 1 and out[0]["approved"] is False and out[0]["summary"] == "Set status to On Hold"
    assert await repo.get_due_outbox("simkl") == []  # nothing is sent yet
    route = respx.post(f"{SIMKL_BASE_URL}/sync/add-to-list").respond(json={})
    assert await sync.flush_outbox() == 0 and not route.called

    await sync.approve_outgoing([out[0]["id"]])
    assert await sync.flush_outbox() == 1 and route.called


@pytest.mark.asyncio
@respx.mock
async def test_discard_restores_simkl_version(env):
    repo, client, sync = env
    mock_reads()
    await sync.sync(deep=False)
    sev = await repo.get_show_by_external_id("simkl", 1400298)
    await sync.update_show_status(sev.id, ShowStatus.DROPPED)
    out = await repo.get_outbox()
    res = await sync.discard_outgoing([out[0]["id"]])
    assert res["discarded"] == 1 and res["restored"] == 1
    assert (await repo.get_show_by_external_id("simkl", 1400298)).status == ShowStatus.WATCHING
    assert await repo.get_outbox() == []


@pytest.mark.asyncio
@respx.mock
async def test_incoming_changes_are_staged_then_accepted(env):
    repo, client, sync = env
    mock_reads()
    await sync.sync(deep=False)

    remote = dict(SEVERANCE, status="hold", user_rating=7, watched_episodes_count=3,
                  seasons=[{"number": 1, "episodes": [{"number": n} for n in (1, 2, 3)]}])
    respx.get(f"{SIMKL_BASE_URL}/sync/activities").respond(json=changed_activities())
    respx.get(f"{SIMKL_BASE_URL}/sync/all-items/shows").respond(json={"shows": [remote]})
    result = await sync.sync(deep=False)
    assert result["staged"] == 1

    # Nothing applied yet
    sev = await repo.get_show_by_external_id("simkl", 1400298)
    assert sev.status == ShowStatus.WATCHING and sev.user_rating == 9
    inc = await sync.get_incoming()
    fields = {c["field"]: c for c in inc["entries"][0]["changes"]}
    assert fields["status"] == {"field": "status", "local": "watching", "remote": "hold"}
    assert fields["rating"]["remote"] == 7
    assert fields["episodes"]["added"] == "S01E03"
    assert "item" not in inc["entries"][0]

    await sync.resolve_incoming(None, accept=True)
    sev = await repo.get_show_by_external_id("simkl", 1400298)
    assert sev.status == ShowStatus.HOLD and sev.user_rating == 7 and sev.watched_episodes == 3
    assert (await sync.get_incoming())["entries"] == []
    assert json.loads(await repo.get_state("driver.simkl.cursor"))["all"] == "2026-10-05T10:00:00Z"


@pytest.mark.asyncio
@respx.mock
async def test_keep_mine_pushes_local_state_back(env):
    repo, client, sync = env
    mock_reads()
    await sync.sync(deep=False)
    remote = dict(SEVERANCE, status="dropped",
                  seasons=[{"number": 1, "episodes": [{"number": 1}]}])
    respx.get(f"{SIMKL_BASE_URL}/sync/activities").respond(json=changed_activities())
    respx.get(f"{SIMKL_BASE_URL}/sync/all-items/shows").respond(json={"shows": [remote]})
    await sync.sync(deep=False)

    key = (await sync.get_incoming())["entries"][0]["key"]
    await sync.resolve_incoming([key], accept=False)
    sev = await repo.get_show_by_external_id("simkl", 1400298)
    assert sev.status == ShowStatus.WATCHING  # local kept
    ops = {o["op"]: o for o in await repo.get_outbox()}
    assert ops["status"]["approved"] and ops["status"]["payload"]["status"] == "watching"
    assert ops["watched"]["summary"] == "Mark S01E02 watched"  # SIMKL lost E02; re-send it


@pytest.mark.asyncio
@respx.mock
async def test_removal_on_simkl_is_staged(env):
    repo, client, sync = env
    mock_reads()
    await sync.sync(deep=False)
    acts = json.loads(json.dumps(ACTIVITIES_1))
    acts["all"] = acts["movies"]["removed_from_list"] = "2026-10-06T10:00:00Z"
    respx.get(f"{SIMKL_BASE_URL}/sync/activities").respond(json=acts)
    respx.get(f"{SIMKL_BASE_URL}/sync/all-items/movies").respond(json={"movies": []})
    result = await sync.sync(deep=False)
    assert result["staged"] == 1
    assert await repo.get_show_by_external_id("simkl", 2222) is not None  # not deleted until accepted
    await sync.resolve_incoming(None, accept=True)
    assert await repo.get_show_by_external_id("simkl", 2222) is None


@pytest.mark.asyncio
@respx.mock
async def test_read_only_token_reported(env):
    repo, client, sync = env
    mock_reads()
    await sync.sync(deep=False)
    await repo.set_state("sync_review_mode", "off")
    sev = await repo.get_show_by_external_id("simkl", 1400298)
    await sync.update_show_status(sev.id, ShowStatus.HOLD)
    respx.post(f"{SIMKL_BASE_URL}/sync/add-to-list").respond(
        status_code=403, json={"error": "insufficient_scope", "code": 403, "message": "Requires media:write"}
    )
    assert await sync.flush_outbox() == 0
    assert "read-only" in await repo.get_state("driver.simkl.last_error")
    assert (await repo.get_outbox())[0]["attempts"] == 0  # not burned on retries


def test_queued_rows_describe_themselves():
    from penguwatch.drivers.base import SyncOp
    from penguwatch.sync.ops import describe
    assert describe(SyncOp(kind="status", media_type="show", ids={"simkl": 1}, status="hold")) == "Set status to On Hold"
    eps = SyncOp(kind="watched", media_type="show", ids={"simkl": 1}, episodes=[(2, 3)])
    assert describe(eps) == "Mark S02E03 watched"
    assert describe(SyncOp(kind="unwatched", media_type="movie", ids={"simkl": 2})) == "Mark movie unwatched"
    assert describe(SyncOp(kind="rate", media_type="movie", ids={"simkl": 2}, rating=8)) == "Rate 8/10"
