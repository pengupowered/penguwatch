"""Insights: the date filters compare the stored text (index-friendly) and give what substr() gave; the aggregates are
reused until the next write."""

import pytest

from penguwatch import clock
from penguwatch.db import reports
from tests.test_insights_shape import _FixedDatetime, build_fixture_repo


async def _legacy_service_value(repo, days):
    """get_service_value before the change (substr of the date column)."""
    from datetime import timedelta
    cutoff = (reports.datetime.now(clock.tz()).date() - timedelta(days=days)).isoformat()
    db = await repo.read_connection()
    out = {}
    for sql in (
        """SELECT s.service_id AS service_id, COUNT(*) AS episodes, COALESCE(SUM(COALESCE(s.runtime_minutes, 45)), 0) / 60.0 AS hours
           FROM episodes e JOIN shows s ON s.id = e.show_id WHERE e.is_watched = 1 AND substr(e.watched_at, 1, 10) >= ?
           GROUP BY s.service_id""",
        """SELECT service_id, COUNT(*) AS episodes, COALESCE(SUM(COALESCE(runtime_minutes, 110)), 0) / 60.0 AS hours
           FROM shows WHERE media_type = 'movie' AND status = 'completed' AND substr(last_watched_at, 1, 10) >= ?
             AND NOT EXISTS (SELECT 1 FROM episodes e WHERE e.show_id = shows.id AND e.is_watched = 1) GROUP BY service_id"""):
        for r in await (await db.execute(sql, (cutoff,))).fetchall():
            row = out.setdefault(r["service_id"] or "", {"episodes": 0, "hours": 0.0})
            row["episodes"] += r["episodes"]
            row["hours"] += r["hours"]
    return out


@pytest.mark.asyncio
async def test_service_value_matches_the_substr_filter_around_the_cutoff(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "datetime", _FixedDatetime)  # "today" is 2026-03-15
    repo = await build_fixture_repo(tmp_path)
    try:
        db = await repo.get_connection()
        sid = (await (await db.execute("SELECT id FROM shows WHERE title = 'Beta'")).fetchone())[0]
        edges = ["2026-02-13", "2026-02-12", "2026-02-13T00:00:00Z", "2026-02-12T23:59:59+00:00", "2026-02-13 08:00:00"]
        await db.executemany("INSERT INTO episodes (show_id, season, number, is_watched, watched_at) VALUES (?, 9, ?, 1, ?)",
                             [(sid, i, w) for i, w in enumerate(edges, start=1)])
        await db.commit()
        for days in (1, 30, 31, 90, 365):
            assert await repo.get_service_value(days) == await _legacy_service_value(repo, days), days
        assert (await repo.get_service_value(30))["max"]["episodes"] >= 4
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_insight_rows_are_reused_until_a_write_and_copied(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "datetime", _FixedDatetime)
    repo = await build_fixture_repo(tmp_path)
    try:
        built = []
        original = repo._insight_rows
        repo._insight_rows = lambda *a: (built.append(a), original(*a))[1]
        first = await repo.get_insights(12)
        second = await repo.get_insights(12)
        assert first == second and len(built) == 1
        first["by_service"].clear()  # a caller changing its copy does not change the next answer
        assert (await repo.get_insights(12)) == second and len(built) == 1
        await repo.get_insights(6)  # another window is its own entry
        assert len(built) == 2
        await repo.set_state("anything", "x")  # any write drops it
        await repo.get_insights(12)
        assert len(built) == 3
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_insight_rows_do_not_outlive_the_day(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(reports, "datetime", _FixedDatetime)
    repo = await build_fixture_repo(tmp_path)
    try:
        before = await repo.get_insights(12)
        class _Later(_FixedDatetime):
            @classmethod
            def now(cls, tz=None):
                later = datetime(2026, 3, 16, 12, 0, tzinfo=timezone.utc)
                return later if tz is None else later.astimezone(tz)
        monkeypatch.setattr(reports, "datetime", _Later)
        await repo.get_insights(12)
        assert [k for k in repo._derived if k.startswith("insight_rows:")] == ["insight_rows:12:2026-03-16"]
        assert before["months"]
    finally:
        await repo.close()
