"""Tidy-up: completed shows with new seasons, and stale 'watching' shows."""

from datetime import date, datetime, timedelta, timezone

import pytest
from httpx import AsyncClient, ASGITransport

from penguwatch.models import Show, ShowStatus
from penguwatch.main import app
from tests.test_api import setup_test_app  # noqa: F401  (fixture)

T = date.today()


async def _show_with_episodes(repo, title, status, seasons, watched_upto, ended=False, last_watched_days=None):
    sid = await repo.upsert_show(Show(title=title, status=status, is_ended=ended, ids={"simkl": hash(title) % 100000}))
    eps = []
    for s_num, (count, first_air) in enumerate(seasons, start=1):
        for n in range(1, count + 1):
            eps.append({"season": s_num, "number": n, "title": f"E{n}", "air_date": (first_air + timedelta(days=n)).isoformat()})
    await repo.upsert_episode_list(sid, eps)
    await repo.set_episodes_watched(sid, [(e["season"], e["number"]) for e in eps if (e["season"], e["number"]) <= watched_upto], True)
    fields = {"episodes_synced_at": datetime.now(timezone.utc).isoformat()}
    if last_watched_days is not None:
        fields["last_watched_at"] = (datetime.now(timezone.utc) - timedelta(days=last_watched_days)).isoformat()
    await repo.update_show_fields(sid, fields)
    await repo.recompute_progress_from_episodes(sid)
    await repo.update_show_fields(sid, {"status": status.value})
    return sid


@pytest.mark.asyncio
async def test_returning_and_stale_detection(setup_test_app):
    repo = setup_test_app
    past = T - timedelta(days=400)
    # Completed; season 2 announced for the future
    upcoming = await _show_with_episodes(repo, "Upcoming S2", ShowStatus.COMPLETED, [(6, past), (8, T + timedelta(days=20))], (1, 6))
    # Completed; season 2 already out
    out_now = await _show_with_episodes(repo, "Out Now", ShowStatus.COMPLETED, [(5, past), (4, T - timedelta(days=30))], (1, 5))
    # Completed and nothing new
    await _show_with_episodes(repo, "Done", ShowStatus.COMPLETED, [(5, past)], (1, 5), ended=True)
    # Watching but idle for a year, and one active
    stale = await _show_with_episodes(repo, "Abandoned", ShowStatus.WATCHING, [(10, past)], (1, 3), last_watched_days=365)
    await _show_with_episodes(repo, "Active", ShowStatus.WATCHING, [(10, past)], (1, 3), last_watched_days=5)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        tidy = (await ac.get("/api/tidy?months=6")).json()
        ret = {r["show"]["title"]: r for r in tidy["returning"]}
        assert set(ret) == {"Upcoming S2", "Out Now"}
        assert ret["Out Now"]["state"] == "available" and ret["Out Now"]["first_code"] == "S02E01"
        assert ret["Upcoming S2"]["state"] == "upcoming" and ret["Upcoming S2"]["new_season"] == 2
        assert [r["show"]["title"] for r in tidy["stale"]] == ["Abandoned"]
        assert tidy["stale"][0]["reason"] == "7 episodes left"

        res = (await ac.post("/api/tidy/apply", json={"show_ids": [out_now, stale], "status": "plantowatch"})).json()
        assert res["moved"] == 2
        assert (await repo.get_show_by_id(out_now)).status == ShowStatus.PLANTOWATCH

        await ac.post("/api/tidy/dismiss", json={"kind": "returning", "keys": [ret["Upcoming S2"]["key"]]})
        tidy = (await ac.get("/api/tidy")).json()
        assert tidy["returning"] == [] and tidy["stale"] == []


@pytest.mark.asyncio
async def test_anime_sequel_suggestion_and_add(setup_test_app):
    import json
    from penguwatch.models import MediaType
    repo = setup_test_app
    sid = await repo.upsert_show(Show(title="Frieren", ids={"simkl": 111}, media_type=MediaType.ANIME, status=ShowStatus.COMPLETED))
    await repo.update_show_fields(sid, {"sequels_json": json.dumps([
        {"ids": {"simkl": 222}, "title": "Frieren S2", "year": 2026, "poster": None, "anime_type": "tv"}
    ])})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        tidy = (await ac.get("/api/tidy")).json()
        seq = [r for r in tidy["returning"] if r.get("kind") == "sequel"]
        assert len(seq) == 1 and seq[0]["sequel"]["title"] == "Frieren S2"
        res = (await ac.post("/api/tidy/apply", json={
            "status": "plantowatch", "sequels": [{"ids": {"simkl": 222}, "title": "Frieren S2", "parent_id": sid}]
        })).json()
        assert res["added"] == 1
        added = await repo.get_show_by_external_id("simkl", 222)
        assert added.status == ShowStatus.PLANTOWATCH and added.media_type == MediaType.ANIME
        assert (await ac.get("/api/tidy")).json()["returning"] == []


def test_extract_sequels_direct_only():
    from tests.simkl_helpers import simkl_engine
    rels = {"relations": [
        {"title": "A", "en_title": "A S2", "year": 2026, "anime_type": "tv", "relation_type": "sequel", "is_direct": True, "ids": {"simkl": 1}},
        {"title": "A", "year": 2027, "anime_type": "tv", "relation_type": "sequel", "is_direct": False, "ids": {"simkl": 2}},
        {"title": "A", "year": 2020, "anime_type": "tv", "relation_type": "prequel", "is_direct": True, "ids": {"simkl": 3}},
    ]}
    from penguwatch.drivers.simkl.mapper import extract_sequels
    from penguwatch.sync.titles import sequel_record
    out = [sequel_record(r) for r in extract_sequels(rels)]
    assert [o["ids"] for o in out] == [{"simkl": 1}] and out[0]["title"] == "A S2"


@pytest.mark.asyncio
async def test_stale_suggestions_per_show(setup_test_app):
    repo = setup_test_app
    past = T - timedelta(days=400)
    done = await _show_with_episodes(repo, "Caught Up Nothing New", ShowStatus.WATCHING, [(5, past)], (1, 5), last_watched_days=300)
    left = await _show_with_episodes(repo, "Half Watched", ShowStatus.WATCHING, [(10, past)], (1, 4), last_watched_days=300)
    waiting = await _show_with_episodes(repo, "Between Episodes", ShowStatus.WATCHING,
                                        [(5, past), (6, T + timedelta(days=3))], (1, 5), last_watched_days=300)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        stale = {r["show"]["title"]: r["suggested_status"] for r in (await ac.get("/api/tidy")).json()["stale"]}
        assert stale == {"Caught Up Nothing New": "completed", "Half Watched": "hold", "Between Episodes": "watching"}
        res = (await ac.post("/api/tidy/apply", json={"moves": [
            {"show_id": done, "status": "completed"}, {"show_id": left, "status": "hold"}]})).json()
        assert res["moved"] == 2
        assert (await repo.get_show_by_id(done)).status == ShowStatus.COMPLETED
        assert (await repo.get_show_by_id(left)).status == ShowStatus.HOLD
        assert (await repo.get_show_by_id(waiting)).status == ShowStatus.WATCHING
