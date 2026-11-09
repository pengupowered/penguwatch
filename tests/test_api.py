"""Integration tests for FastAPI endpoints."""

import pytest
from httpx import AsyncClient, ASGITransport
from penguwatch.main import app


@pytest.fixture
async def setup_test_app(container):
    yield container.repository


@pytest.mark.asyncio
async def test_api_stats_and_services(setup_test_app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Test stats
        res = await ac.get("/api/stats")
        assert res.status_code == 200
        stats = res.json()
        assert stats["active_services_count"] == 1
        assert stats["current_monthly_spend"] == 9.99

        # Test services list
        res_svc = await ac.get("/api/services")
        assert res_svc.status_code == 200
        services = res_svc.json()
        assert len(services) == 2


@pytest.mark.asyncio
async def test_api_shows_crud_and_pick(setup_test_app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Create show
        create_payload = {
            "title": "Ted Lasso",
            "year": 2020,
            "network": "Apple TV+",
            "service_id": "apple_tv",
            "status": "watching",
            "priority": "high",
            "household": ["person1", "person2"],
            "imdb_rating": 8.8,
            "total_episodes": 34,
            "aired_episodes": 34,
            "watched_episodes": 10,
        }
        res_create = await ac.post("/api/shows", json=create_payload)
        assert res_create.status_code == 200
        show = res_create.json()
        assert show["title"] == "Ted Lasso"
        show_id = show["id"]

        # Increment episode
        res_inc = await ac.post(f"/api/shows/{show_id}/increment")
        assert res_inc.status_code == 200
        assert res_inc.json()["watched_episodes"] == 11

        # Test optimizer plan
        res_plan = await ac.get("/api/optimizer/plan")
        assert res_plan.status_code == 200
        plan = res_plan.json()
        assert len(plan["service_rankings"]) == 2

        # Test decision roulette / pick
        pick_payload = {
            "only_active_services": True,
            "min_rating": 8.0,
            "shared": True,
        }
        res_pick = await ac.post("/api/decide/pick", json=pick_payload)
        assert res_pick.status_code == 200
        pick_data = res_pick.json()
        assert pick_data["result"] is not None
        assert pick_data["result"]["title"] == "Ted Lasso"


@pytest.mark.asyncio
async def test_api_show_progress_stepping(setup_test_app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Create a 20-episode show (e.g. 2 seasons of 10)
        res_create = await ac.post(
            "/api/shows",
            json={
                "title": "Severance",
                "year": 2022,
                "service_id": "apple_tv",
                "status": "watching",
                "priority": "high",
                "total_episodes": 20,
                "aired_episodes": 20,
                "watched_episodes": 0,
            },
        )
        assert res_create.status_code == 200
        show_id = res_create.json()["id"]

        # Step +1 episode
        res = await ac.post(f"/api/shows/{show_id}/progress", json={"episode_delta": 1})
        assert res.status_code == 200
        assert res.json()["watched_episodes"] == 1

        # Step -1 episode
        res = await ac.post(f"/api/shows/{show_id}/progress", json={"episode_delta": -1})
        assert res.status_code == 200
        assert res.json()["watched_episodes"] == 0

        # Cannot decrement below 0
        res = await ac.post(f"/api/shows/{show_id}/progress", json={"episode_delta": -1})
        assert res.status_code == 200
        assert res.json()["watched_episodes"] == 0

        # Step +1 season (default 10 eps)
        res = await ac.post(f"/api/shows/{show_id}/progress", json={"season_delta": 1})
        assert res.status_code == 200
        assert res.json()["watched_episodes"] == 10

        # Step +1 season again -> 20
        res = await ac.post(f"/api/shows/{show_id}/progress", json={"season_delta": 1})
        assert res.status_code == 200
        assert res.json()["watched_episodes"] == 20

        # Step -1 season -> 10
        res = await ac.post(f"/api/shows/{show_id}/progress", json={"season_delta": -1})
        assert res.status_code == 200
        assert res.json()["watched_episodes"] == 10

        # Direct set to 15
        res = await ac.post(f"/api/shows/{show_id}/progress", json={"watched_episodes": 15})
        assert res.status_code == 200
        assert res.json()["watched_episodes"] == 15

        # From partial season (15), +1 season should advance to next season boundary (20)
        res = await ac.post(f"/api/shows/{show_id}/progress", json={"season_delta": 1})
        assert res.status_code == 200
        assert res.json()["watched_episodes"] == 20


@pytest.mark.asyncio
async def test_api_up_next_ics_manual_service_and_delete(setup_test_app):
    from datetime import date, timedelta
    repo = setup_test_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post("/api/shows", json={
            "title": "Slow Horses", "service_id": "apple_tv", "status": "watching",
            "total_episodes": 12, "aired_episodes": 12, "watched_episodes": 5,
        })
        show = res.json()
        sid = show["id"]
        assert show["episodes_left"] == 7 and show["is_ready_to_binge"] is True  # computed fields are serialized

        # decrement
        res = await ac.post(f"/api/shows/{sid}/decrement")
        assert res.json()["watched_episodes"] == 4

        # Up Next
        res = await ac.get("/api/up-next")
        assert [s["title"] for s in res.json()] == ["Slow Horses"]

        # manual service pick is remembered
        res = await ac.patch(f"/api/shows/{sid}", json={"service_id": "netflix"})
        assert res.json()["service_id"] == "netflix" and res.json()["service_manual"] is True

        # iCal feed
        await repo.upsert_upcoming_episodes([{
            "show_id": sid, "season": 5, "number": 1, "title": "Return",
            "air_date": (date.today() + timedelta(days=2)).isoformat(),
        }])
        res = await ac.get("/api/calendar.ics")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/calendar")
        assert "SUMMARY:Slow Horses S05E01 – Return" in res.text

        # Episodes checklist endpoint works for local shows too
        res = await ac.get(f"/api/shows/{sid}/episodes")
        assert res.status_code == 200 and res.json()["complete_list"] is False

        # delete a SIMKL-linked title queues the removal on SIMKL
        await repo.update_show_fields(sid, {"ids": {"simkl": 999}})
        res = await ac.delete(f"/api/shows/{sid}")
        assert res.json()["removed_from_library"] is True
        ops = [o["op"] for o in await repo.get_outbox()]
        assert ops == ["remove"]


@pytest.mark.asyncio
async def test_tmdb_routes_are_gone(setup_test_app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.get("/api/settings/tmdb")).status_code in (404, 405)
        assert (await ac.post("/api/settings/tmdb", json={"api_key": "k"})).status_code in (404, 405)
        assert (await ac.post("/api/shows/1/providers/refresh")).status_code in (404, 405)


def test_trending_item_normalization():
    from penguwatch.api.discover import catalog_entry
    from penguwatch.drivers.simkl.mapper import catalog_item
    raw = {
        "title": "Lanterns", "poster": "12/abc", "release_date": "08/17/2026", "network": "HBO",
        "ids": {"simkl_id": 2633100, "tmdb": "1234", "slug": "lanterns"},
        "ratings": {"simkl": {"rating": 7.9, "votes": 10}, "imdb": {"rating": 7.4, "votes": 100}},
        "genres": ["Drama"], "overview": "…", "rank": 1, "watched": 5000,
    }
    it = catalog_entry(catalog_item(raw, "show"))
    assert it["ids"] == {"simkl": 2633100, "tmdb": 1234}
    assert it["year"] == 2026 and it["imdb_rating"] == 7.4
    assert it["poster"] == "https://simkl.in/posters/12/abc_m.jpg"
