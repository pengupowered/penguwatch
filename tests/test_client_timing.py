"""Slow requests as the browser saw them: POST /api/client-timing logs where the time went."""

import logging

import pytest
from httpx import ASGITransport, AsyncClient

from penguwatch import activity
from penguwatch.main import app
from tests.test_api import setup_test_app  # noqa: F401  (fixture)

REPORT = {
    "method": "GET", "path": "/api/up-next", "status": 200, "total_ms": 8423.4, "server_ms": 120.2,
    "page_ms": 15.0, "wait_ms": 7012.0, "connect_ms": 1180.6, "ttfb_ms": 1300.1, "download_ms": 3.2,
    "visible": True, "online": True, "page_age_s": 12.5, "since_visible_s": 9.1, "connection": "4g",
}


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_report_is_logged_with_the_breakdown_and_the_device(setup_test_app, caplog):
    caplog.set_level(logging.WARNING, logger="penguwatch.client")
    async with client() as ac:
        res = await ac.post("/api/client-timing", json=REPORT, headers={"User-Agent": "Mozilla/5.0 (Phone) Test/1.0"})
    assert res.status_code == 204
    line = next(r.getMessage() for r in caplog.records if r.name == "penguwatch.client")
    for part in ("GET /api/up-next", "8423 ms in the browser", "server 120 ms", "page busy before sending 15 ms", "waiting to send 7012 ms",
                 "connect 1181 ms", "first byte 1300 ms", "page open 12 s", "visible again 9 s ago",
                 "4g", "Mozilla/5.0 (Phone) Test/1.0"):
        assert part in line, (part, line)


@pytest.mark.asyncio
async def test_failed_request_and_missing_breakdown(setup_test_app, caplog):
    caplog.set_level(logging.WARNING, logger="penguwatch.client")
    report = {"method": "GET", "path": "/api/stats?x=secret", "status": 0, "total_ms": 30000,
              "visible": False, "online": False, "page_age_s": 3600}
    async with client() as ac:
        res = await ac.post("/api/client-timing", json=report)
    assert res.status_code == 204
    line = next(r.getMessage() for r in caplog.records if r.name == "penguwatch.client")
    assert "GET /api/stats -> network error" in line and "secret" not in line
    assert "hidden" in line and "offline" in line


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {**REPORT, "path": "/static/app.js"},
    {**REPORT, "path": "/api/up-next\nFAKE LOG LINE"},
    {**REPORT, "method": "get; rm"},
    {**REPORT, "total_ms": -5},
    {**REPORT, "connection": "x" * 40},
])
async def test_malformed_reports_are_rejected(setup_test_app, bad):
    async with client() as ac:
        res = await ac.post("/api/client-timing", json=bad)
    assert res.status_code == 422


def test_reports_do_not_count_as_using_the_app():
    """A tab left open whose background poll is slow must not end the SIMKL idle back-off."""
    assert not activity.is_user_interaction("POST", "/api/client-timing")
    assert activity.is_user_interaction("POST", "/api/shows/1/increment")
