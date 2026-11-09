"""get_upcoming_schedule filters on the raw date columns (so the air_date index serves it); same rows as the
date(substr(...)) filters it replaced."""

import os
import shutil
from datetime import timedelta

import pytest

from penguwatch import clock
from penguwatch.db.episodes import format_episode_code  # noqa: F401
from penguwatch.db.repository import DatabaseRepository
from penguwatch.db.services import EFFECTIVELY_ACTIVE_SQL
from penguwatch.db.shows import household_tx
from penguwatch.db.views import _air_window
from penguwatch.models import Show, ShowStatus
from tests.test_api import setup_test_app  # noqa: F401  (fixture)

SEED = os.environ.get("PENGUWATCH_SEED_DIR")


async def legacy_schedule(repo, days_ahead):
    """The query before the change: the dates compared through date(substr(...))."""
    today = clock.today()
    window = ((today - timedelta(days=2)).isoformat(), (today + timedelta(days=int(days_ahead))).isoformat())
    db = await repo.read_connection()
    rows = [dict(r) for r in await (await db.execute(f"""
        SELECT s.id as id, s.title as title, s.en_title as en_title, s.poster as poster, s.network as network,
               s.service_id as service_id, svc.name as service_name, {EFFECTIVELY_ACTIVE_SQL} as is_service_active,
               e.air_date as next_episode_air_date, e.season as next_episode_season, e.number as next_episode_number,
               e.title as next_episode_title, s.priority as priority
        FROM episodes e JOIN shows s ON e.show_id = s.id LEFT JOIN services svc ON s.service_id = svc.id
        WHERE e.air_date IS NOT NULL AND e.season > 0
          AND date(substr(e.air_date, 1, 10)) >= ? AND date(substr(e.air_date, 1, 10)) <= ?
          AND s.status NOT IN ('completed', 'dropped')
        ORDER BY e.air_date ASC""", window)).fetchall()]
    seen = {(r["id"], (r["next_episode_air_date"] or "")[:10], r.get("next_episode_season"), r.get("next_episode_number"))
            for r in rows}
    for fr in await (await db.execute(f"""
        SELECT s.id, s.title, s.en_title, s.poster, s.network, s.service_id, svc.name as service_name,
               {EFFECTIVELY_ACTIVE_SQL} as is_service_active, s.next_episode_air_date, s.next_episode_season,
               s.next_episode_number, s.next_episode_title, s.priority
        FROM shows s LEFT JOIN services svc ON s.service_id = svc.id
        WHERE s.next_episode_air_date IS NOT NULL
          AND date(substr(s.next_episode_air_date, 1, 10)) >= ? AND date(substr(s.next_episode_air_date, 1, 10)) <= ?
          AND s.status NOT IN ('completed', 'dropped')
        ORDER BY s.next_episode_air_date ASC""", window)).fetchall():
        d = dict(fr)
        key = (d["id"], (d["next_episode_air_date"] or "")[:10], d.get("next_episode_season"), d.get("next_episode_number"))
        if key not in seen:
            rows.append(d)
            seen.add(key)
    rows.sort(key=lambda r: (r["next_episode_air_date"] or ""))
    people = await household_tx(db, {r["id"] for r in rows})  # who wants each title (0.17: show_household)
    for r in rows:
        r["household"] = repo._household_of(people.get(r["id"]))
        if r.get("en_title") is None:
            r.pop("en_title", None)
    return rows


def test_window_ends_the_day_after():
    first, end = _air_window(45)
    today = clock.today()
    assert first == (today - timedelta(days=2)).isoformat() and end == (today + timedelta(days=46)).isoformat()


@pytest.mark.asyncio
async def test_schedule_matches_the_substr_filter_at_the_edges(setup_test_app):
    repo = setup_test_app
    today = clock.today()
    day = lambda n: (today + timedelta(days=n)).isoformat()  # noqa: E731
    sid = await repo.upsert_show(Show(title="Edges", status=ShowStatus.WATCHING, ids={"simkl": 7001}))
    forms = [(-3, ""), (-2, ""), (-2, "T00:00:00Z"), (-1, " 10:30:00"), (0, "T20:00:00+02:00"), (45, ""),
             (45, "T23:59:59Z"), (46, ""), (46, "T00:00:00Z"), (30, "")]
    eps = [{"season": 1, "number": i + 1, "title": f"e{i}", "air_date": day(n) + suffix} for i, (n, suffix) in enumerate(forms)]
    eps.append({"season": 1, "number": 50, "title": "undated", "air_date": None})
    eps.append({"season": 1, "number": 51, "title": "empty", "air_date": ""})
    await repo.upsert_episode_list(sid, eps)
    # the show-level next episode too: one inside with a time part, one just outside
    await repo.update_show_fields(sid, {"next_episode_air_date": day(45) + "T12:00:00Z", "next_episode_season": 2,
                                        "next_episode_number": 1})
    other = await repo.upsert_show(Show(title="Out of range", status=ShowStatus.WATCHING, ids={"simkl": 7002}))
    await repo.update_show_fields(other, {"next_episode_air_date": day(46) + "T00:00:00Z", "next_episode_season": 1,
                                          "next_episode_number": 1})
    for days_ahead in (0, 1, 7, 45):
        new = await repo.get_upcoming_schedule(days_ahead=days_ahead)
        assert new == await legacy_schedule(repo, days_ahead)
    got = {r["next_episode_title"] for r in await repo.get_upcoming_schedule(days_ahead=45)}
    assert {"e1", "e2", "e3", "e4", "e5", "e6", "e9"} <= got  # inside, including the last day with a time part
    assert not ({"e0", "e7", "e8", "undated", "empty"} & got)  # -3 days, +46 days, no date


@pytest.mark.skipif(not SEED, reason="needs the sanitized seed library (scripts/test.sh --seed)")
@pytest.mark.asyncio
async def test_schedule_matches_the_substr_filter_on_the_seed(tmp_path):
    db_file = str(tmp_path / "seed.db")
    shutil.copy(f"{SEED}/penguwatch.db", db_file)
    repo = DatabaseRepository(db_file)
    await repo.initialize()
    try:
        for days_ahead in (0, 14, 45, 400):
            new = await repo.get_upcoming_schedule(days_ahead=days_ahead)
            assert new == await legacy_schedule(repo, days_ahead)
    finally:
        await repo.close()
