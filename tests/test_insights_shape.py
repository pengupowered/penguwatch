"""Insights: behaviour pin of get_insights() on a small DB, plus pure tests of build_insights()."""

import json
from datetime import datetime, timezone

import pytest

from penguwatch.db import reports
from penguwatch.db.repository import DatabaseRepository
from penguwatch.models import MediaType, ServiceConfig, Show, ShowStatus

FIXED_NOW = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is None else FIXED_NOW.astimezone(tz)


async def build_fixture_repo(tmp_path) -> DatabaseRepository:
    repo = DatabaseRepository(str(tmp_path / "insights.db"))
    await repo.initialize([
        ServiceConfig(id="netflix", name="Netflix", monthly_cost=10.0),
        ServiceConfig(id="max", name="Max", monthly_cost=12.0),
    ])
    alpha = await repo.upsert_show(Show(ids={"simkl": 1}, title="Alpha", genres=["Drama", "Sci-Fi"],
                                        service_id="netflix", runtime_minutes=30, status=ShowStatus.WATCHING))
    beta = await repo.upsert_show(Show(ids={"simkl": 2}, title="Beta", genres=["Drama", "Comedy"],
                                       service_id="max", runtime_minutes=60, status=ShowStatus.WATCHING))
    gamma = await repo.upsert_show(Show(ids={"simkl": 3}, title="Gamma", genres=["Horror"],
                                        service_id="netflix", runtime_minutes=45, status=ShowStatus.COMPLETED))
    await repo.upsert_show(Show(ids={"simkl": 4}, title="Delta", status=ShowStatus.DROPPED))
    await repo.upsert_show(Show(ids={"simkl": 5}, title="Film", media_type=MediaType.MOVIE, service_id="max",
                                runtime_minutes=100, status=ShowStatus.COMPLETED,
                                last_watched_at="2026-02-20T21:00:00+00:00"))
    db = await repo.get_connection()
    await db.execute("UPDATE shows SET genres_json = 'not json' WHERE id = ?", (gamma,))
    rows = [
        (alpha, 1, 1, 1, "2025-12-30T20:00:00+00:00"),
        (alpha, 1, 2, 1, "2026-01-02T20:00:00+00:00"),
        (alpha, 1, 3, 1, "2026-01-02T22:00:00+00:00"),
        (alpha, 1, 4, 1, "2026-03-10T20:00:00+00:00"),
        (alpha, 1, 5, 1, "2025-01-01T20:00:00+00:00"),  # before the window
        (alpha, 1, 6, 0, None),
        (beta, 1, 1, 1, "2026-03-14T20:00:00+00:00"),
        (beta, 1, 2, 0, None),
        (gamma, 1, 1, 1, "2026-02-01T20:00:00+00:00"),
    ]
    await db.executemany(
        "INSERT INTO episodes (show_id, season, number, is_watched, watched_at) VALUES (?, ?, ?, ?, ?)", rows)
    await db.commit()
    return repo


@pytest.mark.asyncio
async def test_get_insights_payload_is_pinned(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "datetime", _FixedDatetime)
    repo = await build_fixture_repo(tmp_path)
    try:
        payload = await repo.get_insights(12)
    finally:
        await repo.close()
    assert payload == EXPECTED_PAYLOAD
    assert list(payload["by_service"]) == list(EXPECTED_PAYLOAD["by_service"])
    assert [g["genre"] for g in payload["genres"]] == [g["genre"] for g in EXPECTED_PAYLOAD["genres"]]


EXPECTED_PAYLOAD = {
    "active_days_90d": 5,
    "by_service": {
        "max": {"episodes": 2, "hours": 2.666666666666667},
        "netflix": {"episodes": 5, "hours": 2.75},
    },
    "completion_rate": 0.667,
    "genres": [
        {"genre": "Drama", "hours": 3.0},
        {"genre": "Sci-Fi", "hours": 2.0},
        {"genre": "Comedy", "hours": 1.0},
    ],
    "months": [
        {"episodes": 0, "hours": 0.0, "month": "2025-04"},
        {"episodes": 0, "hours": 0.0, "month": "2025-05"},
        {"episodes": 0, "hours": 0.0, "month": "2025-06"},
        {"episodes": 0, "hours": 0.0, "month": "2025-07"},
        {"episodes": 0, "hours": 0.0, "month": "2025-08"},
        {"episodes": 0, "hours": 0.0, "month": "2025-09"},
        {"episodes": 0, "hours": 0.0, "month": "2025-10"},
        {"episodes": 0, "hours": 0.0, "month": "2025-11"},
        {"episodes": 1, "hours": 0.5, "month": "2025-12"},
        {"episodes": 2, "hours": 1.0, "month": "2026-01"},
        {"episodes": 2, "hours": 2.4, "month": "2026-02"},
        {"episodes": 2, "hours": 1.5, "month": "2026-03"},
    ],
    "statuses": {"completed": 2, "dropped": 1, "watching": 2},
    "weekdays": [1, 0, 2, 0, 0, 2, 1],
}


def _rows(**over):
    base = {
        "first": "2025-11-01",
        "episode_months": [
            {"ym": "2025-11", "n": 2, "hours": 1.0},
            {"ym": "2025-12", "n": 1, "hours": None},
            {"ym": "2026-01", "n": 1, "hours": 0.5},
            {"ym": "2024-10", "n": 7, "hours": 9.0},  # outside the window: ignored
        ],
        "movie_months": [{"ym": "2026-01", "n": 1, "hours": 11 / 6}],
        "genre_rows": [
            {"genres_json": '["Drama", "Comedy"]', "hours": 1.0},
            {"genres_json": "not json", "hours": 5.0},
            {"genres_json": '["Comedy", ""]', "hours": 0.3},
            {"genres_json": None, "hours": 2.0},
        ],
        "weekday_rows": [{"dow": None, "n": 9}, {"dow": 0, "n": 2}, {"dow": 6, "n": 4}],
        "active_days": 3,
        "statuses": {"completed": 3, "dropped": 1, "watching": 2},
        "by_service": {"netflix": {"episodes": 4, "hours": 2.5}},
    }
    base.update(over)
    return base


def test_build_insights_shapes_hand_built_rows():
    from penguwatch.insights import build_insights

    out = build_insights(_rows(), 4)
    assert out == {
        "months": [
            {"month": "2025-11", "episodes": 2, "hours": 1.0},
            {"month": "2025-12", "episodes": 1, "hours": 0.0},
            {"month": "2026-01", "episodes": 2, "hours": 2.3},
            {"month": "2026-02", "episodes": 0, "hours": 0.0},
        ],
        "by_service": {"netflix": {"episodes": 4, "hours": 2.5}},
        "genres": [{"genre": "Comedy", "hours": 1.3}, {"genre": "Drama", "hours": 1.0}],
        "weekdays": [2, 0, 0, 0, 0, 0, 4],
        "active_days_90d": 3,
        "completion_rate": 0.75,
        "statuses": {"completed": 3, "dropped": 1, "watching": 2},
    }


def test_build_insights_completion_rate_none_and_year_boundary():
    from penguwatch.insights import build_insights

    out = build_insights(_rows(first="2025-12-01", statuses={"watching": 2}), 3)
    assert out["completion_rate"] is None
    assert [m["month"] for m in out["months"]] == ["2025-12", "2026-01", "2026-02"]


def test_build_insights_genres_truncated_stable():
    from penguwatch.insights import build_insights

    rows = _rows(genre_rows=[{"genres_json": json.dumps([f"G{i:02d}" for i in range(12)]), "hours": 1.0}])
    out = build_insights(rows, 4)
    assert [g["genre"] for g in out["genres"]] == [f"G{i:02d}" for i in range(10)]


def test_api_payload_names_sorts_and_totals():
    from penguwatch.insights import api_payload

    data = {
        "months": [{"month": "2026-01", "episodes": 2, "hours": 1.5}, {"month": "2026-02", "episodes": 3, "hours": 2.0}],
        "by_service": {"a": {"episodes": 1, "hours": 0.44}, "": {"episodes": 2, "hours": 3.0}, "zz": {"episodes": 1, "hours": 1.0}},
    }
    out = api_payload(data, {"a": "Alpha"})
    assert [r["service"] for r in out["by_service"]] == ["Unassigned", "zz", "Alpha"]
    assert out["by_service"][2] == {"service_id": "a", "service": "Alpha", "episodes": 1, "hours": 0.4}
    assert out["total_hours"] == 3.5 and out["total_episodes"] == 5
