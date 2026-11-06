"""Integration tests for GET /api/optimizer/rotation."""

import pytest
from httpx import AsyncClient, ASGITransport

from penguwatch.api.deps import get_optimizer
from penguwatch.main import app
from penguwatch.models import OptimizerPlan


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_rotation_shape(container):
    async with _client() as ac:
        res = await ac.get("/api/optimizer/rotation")
    assert res.status_code == 200
    body = res.json()
    assert body["months"] == 6
    for key in ("slots", "pace_hours_per_day", "pace_floor_applied", "unassigned_titles",
                "summary", "services", "picks", "notes"):
        assert key in body
    assert {s["service_id"] for s in body["services"]} == {"apple_tv", "netflix"}
    assert set(body["summary"]) == {
        "current_monthly_spend", "planned_avg_monthly_spend", "monthly_difference", "cancel_now",
    }
    assert body["summary"]["current_monthly_spend"] == 9.99


@pytest.mark.asyncio
async def test_rotation_months_param(container):
    async with _client() as ac:
        ok = await ac.get("/api/optimizer/rotation", params={"months": 12})
        low = await ac.get("/api/optimizer/rotation", params={"months": 0})
        high = await ac.get("/api/optimizer/rotation", params={"months": 13})
    assert ok.status_code == 200
    assert ok.json()["months"] == 12
    assert low.status_code == 422
    assert high.status_code == 422


@pytest.mark.asyncio
async def test_rotation_without_services(container):
    class _EmptyOptimizer:
        async def calculate_plan(self):
            return OptimizerPlan(
                current_monthly_spend=0.0, all_services_spend=0.0, monthly_savings=0.0,
                annual_savings=0.0, max_active_services=1, active_services=[],
                service_rankings=[], rotation_advice=[],
            )

    app.dependency_overrides[get_optimizer] = lambda: _EmptyOptimizer()
    try:
        async with _client() as ac:
            res = await ac.get("/api/optimizer/rotation")
    finally:
        app.dependency_overrides.pop(get_optimizer, None)
    assert res.status_code == 200
    body = res.json()
    assert body["services"] == []
    assert body["notes"]


@pytest.mark.asyncio
async def test_openapi_lists_rotation_route(container):
    async with _client() as ac:
        res = await ac.get("/openapi.json")
    assert res.status_code == 200
    assert "/api/optimizer/rotation" in res.json()["paths"]
