"""Telemetry: request ids, metrics exposition, /api/system, log redaction."""

import json
import logging

import pytest
from httpx import AsyncClient, ASGITransport

from penguwatch.main import app
from penguwatch.observability import (
    JsonFormatter, Metrics, metrics, normalize_upstream_path, redact, render_prometheus, ContextFilter,
)
from tests.test_api import setup_test_app  # noqa: F401  (fixture)


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_request_id_generated_and_echoed(setup_test_app):
    async with client() as ac:
        res = await ac.get("/api/health")
        assert len(res.headers["x-request-id"]) >= 8
        assert res.headers["server-timing"].startswith("app;dur=")
        echoed = await ac.get("/api/health", headers={"X-Request-ID": "trace-abc.123"})
        assert echoed.headers["x-request-id"] == "trace-abc.123"
        hostile = await ac.get("/api/health", headers={"X-Request-ID": "bad id\twith spaces"})
        assert hostile.headers["x-request-id"] != "bad id\twith spaces"


@pytest.mark.asyncio
async def test_metrics_use_route_templates_not_raw_paths(setup_test_app):
    async with client() as ac:
        await ac.get("/api/shows/424242")  # 404
        await ac.get("/api/shows/777777")
        text = (await ac.get("/metrics")).text
    assert 'route="/api/shows/{show_id}"' in text
    assert "424242" not in text and "777777" not in text
    assert "# TYPE penguwatch_http_request_duration_seconds histogram" in text
    assert 'penguwatch_http_request_duration_seconds_bucket{route="/api/shows/{show_id}",le="+Inf"}' in text
    assert 'status="4xx"' in text
    assert "penguwatch_build_info{version=" in text
    assert "penguwatch_outbox_items{state=\"pending\"}" in text


@pytest.mark.asyncio
async def test_system_endpoint_shape(setup_test_app):
    metrics.record_sync_run("manual", 1_700_000_000.0, {"status": "success", "duration_s": 2.5, "mode": "delta", "pushed": 1})
    async with client() as ac:
        await ac.get("/api/stats")
        data = (await ac.get("/api/system")).json()
    assert data["version"] and data["uptime_seconds"] >= 0
    assert {"database", "sync", "http", "upstream", "logs", "security"} <= data.keys()
    assert data["sync"]["history"][0]["trigger"] == "manual"
    assert data["http"]["requests"] >= 1
    # metrics are process-wide and the endpoint lists only the 12 slowest routes, so check the
    # registry for the route and the endpoint for ordering
    assert any(r["route"] == "/api/stats" for r in metrics.route_summary())
    p95 = [r["p95_ms"] for r in data["http"]["routes"]]
    assert p95 == sorted(p95, reverse=True) and len(p95) <= 12
    assert data["security"]["origin_check"] is True


@pytest.mark.asyncio
async def test_readiness_probe(setup_test_app):
    async with client() as ac:
        res = await ac.get("/api/health/ready")
    assert res.status_code == 200 and res.json()["status"] == "ready"


def test_upstream_paths_are_normalised():
    assert normalize_upstream_path("/tv/12345/seasons") == "/tv/{id}/seasons"
    assert normalize_upstream_path("https://api.simkl.com/anime/987?x=1") == "/anime/{id}"
    assert normalize_upstream_path("/sync/activities") == "/sync/activities"


def test_upstream_summary_counts_errors():
    m = Metrics()
    m.observe_upstream("simkl", "/sync/activities", 200, 0.12)
    m.observe_upstream("simkl", "/sync/activities", 429, 0.05)
    m.observe_upstream("simkl", "/tv/1", 0, 0.5)
    s = m.upstream_summary()["simkl"]
    assert s["requests"] == 3 and s["errors"] == 2 and s["by_status"]["429"] == 1
    text = render_prometheus(m, [], "0.0.0")
    assert 'penguwatch_upstream_requests_total{service="simkl",endpoint="/tv/{id}",status="0"} 1' in text


def test_secrets_are_redacted_from_logs():
    line = "GET https://api.themoviedb.org/3/tv/1?api_key=abcdef0123456789&x=1 Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc"
    cleaned = redact(line)
    assert "abcdef0123456789" not in cleaned and "eyJhbGciOiJIUzI1NiJ9" not in cleaned
    assert "api_key=***" in cleaned and "x=1" in cleaned


def test_json_log_format_and_filter(caplog):
    rec = logging.LogRecord("penguwatch.test", logging.WARNING, __file__, 1, "token=%s used", ("supersecretvalue",), None)
    assert ContextFilter().filter(rec)
    out = json.loads(JsonFormatter().format(rec))
    assert out["level"] == "warning" and out["msg"] == "token=*** used" and "request_id" in out


@pytest.mark.asyncio
async def test_system_endpoint_reports_idle_backoff(setup_test_app):
    async with client() as ac:
        sync = (await ac.get("/api/system")).json()["sync"]
    assert isinstance(sync["idle"], bool)
    assert sync["effective_interval_minutes"] in (sync["interval_minutes"], max(sync["interval_minutes"], 180))
    assert sync["last_interaction_age_seconds"] >= 0
