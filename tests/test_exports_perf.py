"""Exports (CSV, backup), compression and static caching."""

import sqlite3

import pytest
from httpx import AsyncClient, ASGITransport

from penguwatch.main import app
from penguwatch.models import Show, ShowStatus
from tests.test_api import setup_test_app  # noqa: F401  (fixture)


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_csv_export_neutralises_formulas(setup_test_app):
    repo = setup_test_app
    await repo.upsert_show(Show(title='=HYPERLINK("http://evil.example","click")', year=2020, status=ShowStatus.WATCHING))
    await repo.upsert_show(Show(title="Normal, With \"Quotes\"", year=2021))
    async with client() as ac:
        res = await ac.get("/api/export/library.csv")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv")
    assert "attachment" in res.headers["content-disposition"]
    assert "'=HYPERLINK" in res.text and "\n=HYPERLINK" not in res.text
    assert '"Normal, With ""Quotes"""' in res.text


@pytest.mark.asyncio
async def test_backup_is_consistent_and_has_no_credentials(setup_test_app, tmp_path):
    repo = setup_test_app
    await repo.upsert_show(Show(title="Keep Me", status=ShowStatus.WATCHING))
    await repo.set_state("driver.simkl.access_token", "SECRET-TOKEN-VALUE")
    await repo.set_state("driver.simkl.refresh_token", "SECRET-KEY-VALUE")
    await repo.set_state("sync_review_mode", "on")
    async with client() as ac:
        res = await ac.get("/api/export/backup.db")
    assert res.status_code == 200
    out = tmp_path / "backup.db"
    out.write_bytes(res.content)
    conn = sqlite3.connect(out)
    assert conn.execute("select count(*) from shows where title='Keep Me'").fetchone()[0] == 1
    keys = {r[0] for r in conn.execute("select key from app_state")}
    assert "sync_review_mode" in keys and "driver.simkl.access_token" not in keys and "driver.simkl.refresh_token" not in keys
    assert b"SECRET-TOKEN-VALUE" not in res.content and b"SECRET-KEY-VALUE" not in res.content
    conn.close()
    # the live database still has them
    assert await repo.get_state("driver.simkl.access_token") == "SECRET-TOKEN-VALUE"


@pytest.mark.asyncio
async def test_large_json_is_gzipped(setup_test_app):
    repo = setup_test_app
    for i in range(60):
        await repo.upsert_show(Show(title=f"Show number {i}", overview="x" * 400, status=ShowStatus.WATCHING))
    async with client() as ac:
        res = await ac.get("/api/shows", headers={"Accept-Encoding": "gzip"})
    assert res.headers.get("content-encoding") == "gzip"
    assert len(res.json()) >= 60  # httpx transparently decompressed


@pytest.mark.asyncio
async def test_static_assets_are_cacheable(setup_test_app):
    async with client() as ac:
        res = await ac.get("/static/theme.js")
    assert res.status_code == 200
    assert "max-age" in res.headers.get("cache-control", "")


@pytest.mark.asyncio
async def test_insights_aggregates_monthly_hours(setup_test_app):
    from datetime import datetime, timedelta, timezone

    repo = setup_test_app
    sid = await repo.upsert_show(Show(title="Binged", service_id="apple_tv", status=ShowStatus.WATCHING, runtime_minutes=60,
                                      genres=["Drama", "Mystery"]))
    db = await repo.get_connection()
    now = datetime.now(timezone.utc)
    for n in range(1, 5):  # four one-hour episodes this month (seconds apart: hours would cross into last month
        # during the first hours of a month)
        await db.execute("INSERT INTO episodes (show_id, season, number, is_watched, watched_at) VALUES (?, 1, ?, 1, ?)",
                         (sid, n, (now - timedelta(seconds=n)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    await db.commit()
    async with client() as ac:
        data = (await ac.get("/api/insights?months=6")).json()
        assert (await ac.get("/api/insights?months=99")).status_code == 422  # bounded
    assert len(data["months"]) == 6
    assert data["months"][-1]["hours"] == 4.0 and data["months"][-1]["episodes"] == 4
    assert data["total_hours"] == 4.0
    assert {g["genre"] for g in data["genres"]} == {"Drama", "Mystery"}
    assert data["by_service"][0]["service"] == "Apple TV+"
    assert sum(data["weekdays"]) == 4


@pytest.mark.asyncio
async def test_asset_urls_carry_a_content_hash(setup_test_app):
    import re

    async with client() as ac:
        html = (await ac.get("/")).text
    versions = set(re.findall(r"/static/v/([0-9a-f]{10})/(?:theme\.js|js/main\.js|panels/system/panel\.js|panels/insights/panel\.js)", html))
    assert len(versions) == 1, html  # one shared content hash, not the release number
