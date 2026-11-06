"""PUT /api/services/{id}/toggle: omitted renewal fields keep, explicit null clears, a value sets."""

import re

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch.main import app


@pytest.fixture
async def client(container):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def _set(container, **fields):
    await container.repository.set_service_active("apple_tv", True, **fields)


@pytest.mark.asyncio
async def test_omitted_renewal_fields_keep_stored_values(client, container):
    await _set(container, renewal_day=12, renewal_date="2026-11-12")
    r = await client.put("/api/services/apple_tv/toggle", json={"is_active": False})
    assert r.status_code == 200
    body = r.json()
    assert body["is_active"] is False
    assert body["renewal_day"] == 12
    assert body["renewal_date"] == "2026-11-12"


@pytest.mark.asyncio
async def test_explicit_null_clears_renewal_fields(client, container):
    await _set(container, renewal_day=12, renewal_date="2026-11-12")
    r = await client.put("/api/services/apple_tv/toggle",
                         json={"is_active": True, "renewal_day": None, "renewal_date": None})
    body = r.json()
    assert body["renewal_day"] is None
    assert body["renewal_date"] is None


@pytest.mark.asyncio
async def test_null_clears_only_the_named_field(client, container):
    await _set(container, renewal_day=12, renewal_date="2026-11-12")
    r = await client.put("/api/services/apple_tv/toggle", json={"is_active": True, "renewal_day": None})
    body = r.json()
    assert body["renewal_day"] is None
    assert body["renewal_date"] == "2026-11-12"


@pytest.mark.asyncio
async def test_value_sets_renewal_day(client, container):
    r = await client.put("/api/services/apple_tv/toggle", json={"is_active": True, "renewal_day": 5})
    assert r.json()["renewal_day"] == 5


@pytest.mark.asyncio
async def test_unknown_service_is_404(client):
    r = await client.put("/api/services/nope/toggle", json={"is_active": True, "renewal_day": None})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_template_shows_unused_pill_not_annual_savings(client):
    html = (await client.get("/")).text
    assert "Annual Savings" not in html
    # the header's figure is the monthly unused cost: "Unused $x/mo"
    assert re.search(r'<span class="spend-lbl">Unused</span> <b class="num" id="head-unused">\$0\.00</b><span class="spend-unit">/mo</span>', html)
