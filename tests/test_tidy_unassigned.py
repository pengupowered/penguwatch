"""Tidy: the 'assign a service' group and its apply path."""

import pytest
from httpx import AsyncClient, ASGITransport

from penguwatch.models import Show, ShowStatus
from penguwatch.main import app
from tests.test_api import setup_test_app  # noqa: F401  (fixture)


async def _add(repo, title, status=ShowStatus.WATCHING, watched=0, network=None, service_id=None, runtime=60):
    return await repo.upsert_show(Show(
        title=title, status=status, watched_episodes=watched, runtime_minutes=runtime,
        network=network, service_id=service_id, ids={"simkl": abs(hash(title)) % 100000},
    ))


async def _group(ac):
    return (await ac.get("/api/tidy")).json()["unassigned"]


@pytest.mark.asyncio
async def test_group_present_and_empty(setup_test_app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert await _group(ac) == []


@pytest.mark.asyncio
async def test_ordering_exclusions_and_suggestion(setup_test_app):
    repo = setup_test_app
    await _add(repo, "Light", watched=1)
    heavy = await _add(repo, "Heavy", watched=10, network="Apple TV+")
    await repo.update_show_fields(heavy, {"service_id": None})  # upsert auto-assigns from the network
    await _add(repo, "Medium", watched=5, status=ShowStatus.PLANTOWATCH)
    await _add(repo, "Done", status=ShowStatus.COMPLETED, watched=50)
    await _add(repo, "Dropped", status=ShowStatus.DROPPED, watched=50)
    await _add(repo, "Assigned", watched=40, service_id="netflix")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        g = await _group(ac)
    assert [r["title"] for r in g] == ["Heavy", "Medium", "Light"]
    assert g[0]["hours_watched"] == 10.0
    assert g[0]["suggested_service_id"] == "apple_tv"
    assert g[1]["suggested_service_id"] is None
    assert set(g[0]) == {"id", "title", "status", "hours_watched", "interest_score", "suggested_service_id"}


@pytest.mark.asyncio
async def test_cap_at_sixty(setup_test_app):
    repo = setup_test_app
    for i in range(65):
        await _add(repo, f"Show {i:02d}", watched=i)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        g = await _group(ac)
    assert len(g) == 60
    assert g[0]["title"] == "Show 64"


@pytest.mark.asyncio
async def test_apply_sets_service_and_manual_flag(setup_test_app):
    repo = setup_test_app
    sid = await _add(repo, "Pick Me", watched=3)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post("/api/tidy/apply", json={"assignments": [{"show_id": sid, "service_id": "netflix"}]})
        assert res.status_code == 200 and res.json()["assigned"] == 1
        assert await _group(ac) == []
    show = await repo.get_show_by_id(sid)
    assert show.service_id == "netflix" and show.service_manual is True


@pytest.mark.asyncio
async def test_apply_rejects_unknown_service_and_show(setup_test_app):
    repo = setup_test_app
    sid = await _add(repo, "Stay Put", watched=3)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/api/tidy/apply", json={"assignments": [{"show_id": sid, "service_id": "nope"}]})
        assert r.status_code == 422
        r = await ac.post("/api/tidy/apply", json={"assignments": [{"show_id": 99999, "service_id": "netflix"}]})
        assert r.status_code == 404
    show = await repo.get_show_by_id(sid)
    assert show.service_id is None and show.service_manual is False
